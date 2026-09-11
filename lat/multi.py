"""Merge several photo sources into one locals-and-tourists input set.

The single-source build (`lat/build.py` -> `lat/build_fischer.py`) reads one
table, YFCC100M, whose four columns happen to be exactly what Fischer's method
needs: photographer id, date taken, longitude, latitude. Every other source we
can reach is missing or distorting one of those four, and each in a different
way. This module exists to make those differences explicit rather than
averaging them away:

  - ids live in different namespaces, so they are prefixed with their source
    and never reconciled (see MERGE.md);
  - dates mean different things (taken / observed / uploaded), which changes
    what a 30-day span even measures, so `date_kind` travels with every row and
    the residency test can be restricted to kinds where a span means presence;
  - positional precision ranges from a GPS fix to a 22 km randomisation
    (iNaturalist obscuring), so every row carries an estimated uncertainty in
    metres on one common scale;
  - the same photograph reaches us twice by different routes (GBIF republishes
    iNaturalist; Commons holds bot-transferred Flickr files), so rows are
    deduplicated across sources and the removals reported per pair.

Nothing here modifies the single-source path. `lat.classify` is used as-is: it
takes records as dicts with keys user/lon/lat/date, and our rows are supersets
of that contract. `lat.fischer` renders them, with `precision_level` copied
into the `acc` key so its existing accuracy >= 12 line gate applies unchanged.

Column layouts differ per source and are not known when this was written -
four other agents are producing them concurrently. So readers are
header-driven with an alias table, fall back to the YFCC positional layout
when there is no header, and report what they resolved. A source whose files
are absent is reported as absent, not an error.
"""
import csv
import glob
import hashlib
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lat.classify import classify_users, LOCAL, TOURIST, UNKNOWN
from lat.fischer import HANOI_BOUNDS, LINE_MIN_ACCURACY as LINE_GATE_LEVEL

MULTI_DIR = "data/multi"

# Flickr launched in Feb 2004; anything earlier is broken EXIF, and one bogus
# early date stretches a span past a month and mislabels a local (the reason
# lat/build.py clips at all). The upper bound cannot be YFCC's 2015 here:
# Commons, iNaturalist, Panoramax and OSM notes are live sources and a 2015 cap
# would delete nearly all of their rows. So the top of the window is "now".
DATE_MIN = datetime(2003, 1, 1)
DATE_MAX = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0) \
    + timedelta(days=1)

# ---------------------------------------------------------------- date kinds
# What the timestamp is evidence of.
TAKEN = "taken"        # shutter time (Flickr date-taken, Panoramax capture)
OBSERVED = "observed"  # observed_on (iNaturalist, GBIF) - a day, not a moment
UPLOAD = "upload"      # when the file reached the server (Commons dumps)
UNSTATED = "unstated"  # the source shipped the row without saying which

# The harvesters write their own vocabulary into a `date_kind` column, and it
# is not ours: observed_on, created_at, event, uploaded, unknown. Mapping it is
# not cosmetic - "created_at" on iNaturalist and "uploaded" on Commons are
# upload times, and reading either as a capture time is exactly the error the
# date_kind field exists to prevent. An unrecognised value becomes UNSTATED
# rather than falling back to the source default, so a new word shows up in the
# load report instead of being silently promoted to a capture date.
DATE_KIND_ALIASES = {
    "taken": TAKEN, "datetaken": TAKEN, "captured": TAKEN, "capture": TAKEN,
    "capturedat": TAKEN, "datetimeoriginal": TAKEN, "shot": TAKEN,
    "exif": TAKEN,
    "observed": OBSERVED, "observedon": OBSERVED, "event": OBSERVED,
    "eventdate": OBSERVED, "timeobservedat": OBSERVED,
    "upload": UPLOAD, "uploaded": UPLOAD, "dateupload": UPLOAD,
    "dateuploaded": UPLOAD, "uploadtime": UPLOAD, "imgtimestamp": UPLOAD,
    "createdat": UPLOAD, "created": UPLOAD, "ingested": UPLOAD,
    "unknown": UNSTATED, "unstated": UNSTATED, "na": UNSTATED,
}

# Residency asks "was this person here across a span of >= 30 days". Upload
# time cannot answer it in either direction: a two-day visitor who uploads
# their trip over three months reads as a local, and a resident who
# batch-uploads a decade of photographs in one evening reads as unknown. On
# Commons that failure is not hypothetical - bulk transfers and GLAM batches
# are exactly the concentrated uploaders that dominate the Hanoi geosearch.
# So upload-dated rows are plotted but excluded from the span computation by
# default, and a photographer with no residency-eligible row is UNKNOWN, which
# is what yellow already means: no span could be established anywhere.
#
# This is a per-ROW rule, not a per-source one, and that turned out to matter:
# the Commons harvest is 17,168 capture dates against 400 upload dates, so
# excluding the source would have thrown away 98% of it to guard against 2%.
# UNSTATED is excluded on the same principle - a date whose meaning the source
# would not state cannot support a residency claim.
RESIDENCY_DATE_KINDS = (TAKEN, OBSERVED)

# ---------------------------------------------------------------- precision
# One common scale, in metres of positional uncertainty radius, because that
# is the only quantity all five sources can be expressed in. It is then mapped
# back onto Flickr's 1-16 accuracy ladder for lat.fischer's line gate, so the
# existing gate keeps its existing meaning on Flickr rows.
#
# Flickr publishes only three anchors for its ladder (16 street, 11 city,
# 6 region, 3 country, 1 world). The metre values between them are our
# interpolation and are a judgement call; level 12 is pinned to 1000 m so that
# "precision_m <= 1000" is exactly "acc >= 12" and the existing gate is
# reproduced rather than reinterpreted.
FLICKR_LEVEL_METRES = {
    16: 50.0, 15: 100.0, 14: 300.0, 13: 600.0, 12: 1000.0,
    11: 3000.0, 10: 6000.0, 9: 12000.0, 8: 25000.0, 7: 50000.0,
    6: 100000.0, 5: 200000.0, 4: 400000.0, 3: 800000.0,
    2: 1500000.0, 1: 3000000.0,
}

# Rows coarser than this are dropped from mapping outright: the drawn box is
# 24.23 km square, so a position uncertain by 5 km could be anywhere in a fifth
# of the canvas and the mark asserts a place nobody stood. iNaturalist's
# obscuring (~22 km) fails this by a factor of four; so does any coordinate
# stored to 1 decimal place (11 km).
PRECISION_DROP_M = 5000.0
# Coarser than this is plotted but flagged `coarse`, and excluded from
# connecting lines. 1000 m == Flickr accuracy 12, i.e. the existing gate.
PRECISION_COARSE_M = 1000.0

# iNaturalist randomises an obscured observation inside a 0.2 degree cell,
# about 22 km at this latitude - larger than the whole map. Such a coordinate
# is not a coarse measurement of where the observer was, it is a deliberately
# false one, so it can never be plotted. Enforced by a post-condition in
# `filter_precision`, not by convention.
INAT_OBSCURED_DEG = 0.2
INAT_OBSCURED_M = INAT_OBSCURED_DEG * 111320.0

# ---------------------------------------------------------------- sources
class SourceSpec:
    """What we know about one source before reading a single row.

    `date_kind` is the default when the file does not say; `precision_kind`
    says how to read whatever precision column turns up ("level" = Flickr's
    1-16 ladder, "metres", "degrees", or None = infer from coordinate decimal
    places); `floor_m` is the best precision the source can physically carry,
    used when nothing in the row constrains it.
    """

    def __init__(self, key, label, date_kind, precision_kind=None,
                 floor_m=100.0, note="", unknown_m=None, default_on=True,
                 excluded_because="", namespace=None):
        self.key = key
        # The identity system the ids belong to, which is NOT the harvest
        # route. Commons uploader names mean the same thing whether they
        # arrive through the API, the SQL dumps or a Wikidata query, so those
        # three share the `commons:` namespace and one uploader is one
        # photographer across all of them. Two different platforms never
        # share a namespace - see MERGE.md.
        self.namespace = namespace or key
        self.label = label
        self.date_kind = date_kind
        self.precision_kind = precision_kind
        self.floor_m = floor_m
        self.note = note
        # What a row with no precision stated is worth. Not the floor: a blank
        # accuracy column is not evidence of a good fix. Defaults to the coarse
        # threshold, so such rows are plotted, flagged, and kept out of the
        # connecting lines and the coordinate dedup.
        self.unknown_m = PRECISION_COARSE_M if unknown_m is None else unknown_m
        self.default_on = default_on
        self.excluded_because = excluded_because

    def __repr__(self):
        return f"SourceSpec({self.key}, {self.date_kind})"


SOURCES = {
    # The YFCC100M path the single-source build already uses, read through
    # lat.build so there is exactly one date-cleaning implementation.
    "flickr": SourceSpec(
        "flickr", "Flickr / YFCC100M", TAKEN, "level", 50.0,
        "date taken; geo accuracy 1-16 native"),
    "commons": SourceSpec(
        "commons", "Wikimedia Commons (API)", TAKEN, None, 100.0,
        "DateTimeOriginal where present; API geosearch"),
    # The dump route reads the coordinate/upload tables, where img_timestamp is
    # the upload. Kept a separate source precisely so it can be compared with,
    # or substituted for, the API route.
    "commonsdump": SourceSpec(
        "commonsdump", "Wikimedia Commons (SQL dumps)", UPLOAD, None, 100.0,
        "img_timestamp is upload time, not capture", namespace="commons"),
    # Wikidata items whose images are Commons files: a third route to the same
    # platform, so the same uploader namespace.
    "wikidata": SourceSpec(
        "wikidata", "Wikidata (images on Commons)", TAKEN, None, 100.0,
        "coordinates are DMS-derived and no accuracy is stated",
        namespace="commons"),
    "inat": SourceSpec(
        "inat", "iNaturalist", OBSERVED, "metres", 5.0,
        "observed_on is a date; positional_accuracy in metres, blank on "
        "3,660 of 19,866 harvested rows"),
    # Both of the following are kept readable and switched off by default. The
    # reason is recorded here and in MERGE.md so the negative result survives:
    # deleting the reader would leave nothing to stop the next person
    # harvesting them again.
    "gbif": SourceSpec(
        "gbif", "GBIF", OBSERVED, "metres", 5.0, default_on=False,
        note="republishes iNaturalist; coordinateUncertaintyInMeters",
        excluded_because=(
            "48.8% of harvested rows state no uncertainty and the stated ones "
            "have a p90 of 16.4 km (1 sample row at 7,296 m is 1,846 px of "
            "slop on this map); `recordedBy` is free text, not a stable "
            "photographer id, so it cannot anchor a residency test; 863 rows "
            "are 1880s museum specimens; and 3,759 coordinate+day keys are "
            "shared with the iNaturalist harvest, which we already hold at "
            "better fidelity")),
    "osmnotes": SourceSpec(
        "osmnotes", "OSM notes", TAKEN, None, 50.0, default_on=False,
        note="note creation date; hand-placed positions",
        excluded_because=(
            "an OSM note is not a photograph. Its position is where the "
            "mapper says something needs fixing, which is routinely filed "
            "from aerial imagery at a desk, and its `created` date is a "
            "filing time. 2,334 rows from 1,185 accounts is good diversity, "
            "so it is one flag away (--sources osmnotes) if the map is ever "
            "redefined as 'people who recorded something here'")),
    "openaerialmap": SourceSpec(
        "openaerialmap", "OpenAerialMap", UNSTATED, "metres", 1.0,
        default_on=False, note="aerial/satellite imagery footprints",
        excluded_because=(
            "aerial imagery has no photographer standing anywhere: the "
            "coordinate is a scene centroid under an aircraft or satellite, "
            "`accuracy` is a ground sample distance in metres per pixel, and "
            "`acquisition_start` is a flight window. 2 rows in the box")),
    "panoramax": SourceSpec(
        "panoramax", "Panoramax", TAKEN, "metres", 5.0, default_on=False,
        note="street-level imagery, GPS positions",
        excluded_because=(
            "320 rows in the drawn box from exactly 1 contributor, whose "
            "34,327-row history file is that one person's global track. A "
            "locals-vs-tourists distinction cannot be drawn from a single "
            "identity at any coordinate quality")),
    "mapillary": SourceSpec(
        "mapillary", "Mapillary", TAKEN, "metres", 10.0,
        "captured_at; computed GPS"),
}

# When the same photograph arrives twice, which copy is authoritative. Ordered
# best first. The order is a judgement call and is the one thing in the dedup
# that changes which rows survive, so it is stated here rather than implied by
# dict order: prefer capture time over upload time, a primary publisher over a
# republisher, and an API read over a dump read (the dump gives upload dates,
# so keeping the dump copy would downgrade a row's date_kind).
SOURCE_PRIORITY = ["flickr", "panoramax", "mapillary", "inat", "commons",
                   "commonsdump", "gbif", "osmnotes"]


def namespace_of(src):
    sp = SOURCES.get(src)
    return sp.namespace if sp else src


def namespaced_user(src, user):
    """`inat:benjamin`, `commons:Orizan`, `flickr:7997148@N05`.

    The prefix is the identity system, so a Commons uploader called
    "7997148@N05" and the Flickr NSID 7997148@N05 stay two people, and no
    attempt is made to notice that they might be one.
    """
    return f"{namespace_of(src)}:{user}"


def _priority(src):
    try:
        return SOURCE_PRIORITY.index(src)
    except ValueError:
        return len(SOURCE_PRIORITY)


# ---------------------------------------------------------------- column aliases
# Header names seen or plausible across the five routes. Matching is on the
# header lowercased with non-alphanumerics stripped, so "Decimal Longitude",
# "decimalLongitude" and "decimal_longitude" all land on the same alias.
ALIASES = {
    "user": ["user", "userid", "usernsid", "nsid", "username", "uploader",
             "uploadername", "observer", "observerlogin", "observerid",
             "userlogin", "login", "photographer", "author", "creator",
             "recordedby", "contributor", "account", "owner", "actor",
             # Commons SQL dump column names, verbatim
             "imgusertext", "imguser", "actorname", "revusertext"],
    "id": ["id", "photoid", "fileid", "pageid", "imgname", "filename", "file",
           "title", "uuid", "observationid", "occurrenceid", "gbifid", "key",
           "noteid", "pictureid", "sequenceid", "mediaid", "identifier"],
    "lon": ["lon", "lng", "long", "longitude", "decimallongitude", "gtlon",
            "x", "coordlon", "geolon", "pointlon"],
    "lat": ["lat", "latitude", "decimallatitude", "gtlat", "y", "coordlat",
            "geolat", "pointlat"],
    "date": ["date", "datetime", "datetaken", "taken", "timestamp",
             "imgdate", "imgtimestamp", "observedon", "timeobservedat",
             "eventdate", "dateupload", "dateuploaded", "uploaddate",
             "uploadtime", "capturedat", "createdat", "created", "datetimeoriginal",
             "datetimeoriginaldate", "when"],
    "precision": ["acc", "accuracy", "geoaccuracy", "accuracylevel",
                  "flickraccuracy", "precision", "positionalaccuracy",
                  "publicpositionalaccuracy", "coordinateuncertaintyinmeters",
                  "coordinateprecision", "precisionm", "uncertaintym",
                  "gpsprecision", "horizontalaccuracy", "prec",
                  # pos_accuracy is what the iNaturalist harvest actually
                  # ships; missing it silently reduced every iNat row to the
                  # decimal-place fallback, which reads a 7-dp coordinate as a
                  # 5 m fix whatever its stated uncertainty was.
                  "posaccuracy", "poserror", "coordinateaccuracy"],
    "obscured": ["obscured", "isobscured", "geoprivacy", "taxongeoprivacy",
                 "coordinatesobscured", "privacy"],
    "date_kind": ["datekind", "dtkind", "timekind", "datetype"],
    # Free-text columns that can carry the original photograph's provenance.
    "artist": ["artist", "extmetadataartist", "credit", "source", "sourceurl",
               "descriptionurl", "attribution", "references", "occurrenceurl",
               "url", "originalurl", "permalink", "description"],
}
_ALIAS_TO_FIELD = {a: f for f, al in ALIASES.items() for a in al}

# Header names that pin down what the date means regardless of the source
# default: a Commons table that does carry DateTimeOriginal should be read as
# a capture time even though the dump route usually gives uploads.
_DATE_KIND_BY_NAME = [
    ("dateupload", UPLOAD), ("dateuploaded", UPLOAD), ("uploaddate", UPLOAD),
    ("uploadtime", UPLOAD), ("imgtimestamp", UPLOAD),
    ("observedon", OBSERVED), ("timeobservedat", OBSERVED),
    ("eventdate", OBSERVED),
    ("datetaken", TAKEN), ("datetimeoriginal", TAKEN), ("capturedat", TAKEN),
]


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _looks_like_header(fields):
    """A header row has no field parseable as a coordinate in a lon/lat slot.

    Cheap and sufficient: every one of these tables has a longitude column, and
    no source names a column "105.85".
    """
    named = sum(1 for f in fields if _norm(f) in _ALIAS_TO_FIELD)
    if named >= 3:
        return True
    numeric = 0
    for f in fields:
        try:
            float(f)
            numeric += 1
        except (TypeError, ValueError):
            pass
    return named >= 2 and numeric == 0


# The YFCC layout, used when a file has no header at all: the same positional
# columns lat/build.py reads.
YFCC_POSITIONS = {"id": 0, "user": 1, "date": 3, "lon": 4, "lat": 5,
                  "precision": 6}


def _sniff_delim(line):
    return "," if line.count(",") > line.count("\t") else "\t"


# ---------------------------------------------------------------- parsing
_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d", "%Y-%m", "%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
    # JavaScript Date.toString(), after the GMT suffix is normalised above
    "%a %b %d %Y %H:%M:%S",
]


def parse_date(s):
    """-> naive datetime in UTC, or None.

    Timezone-aware stamps are converted to UTC and stripped, because
    lat.classify measures spans against an explicit 1970 epoch and mixing
    aware and naive values there would raise; leaving an offset in place would
    also make a span depend on the photographer's travel, not their dates.
    Sub-day precision is not invented: a bare "2019-05" becomes the 1st, which
    can shorten or lengthen a span by up to a month and is why such rows are
    counted separately in the load report.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.startswith("0000"):
        return None
    s = s.replace("Z", "+00:00")
    # JavaScript Date.toString(), which the iNaturalist harvest emitted for
    # 7,683 history rows: "Fri Sep 11 2015 13:05:25 GMT-0400 (EDT)". Left
    # unhandled these all failed to parse and were silently counted as bad
    # dates, discarding the better of the two iNaturalist history harvests.
    # Drop the trailing timezone name and normalise "GMT-0400" to "-04:00" so
    # the offset regex below can pick it up.
    s = re.sub(r"\s*\([A-Za-z ]+\)\s*$", "", s)
    s = re.sub(r"\bGMT([+-])(\d{2}):?(\d{2})$", r"\1\2:\3", s).strip()
    s = re.sub(r"\bGMT$", "", s).strip()
    # trailing ".0" as YFCC writes it, and any fractional seconds
    s = re.sub(r"\.\d+(?=($|[+-]))", "", s)
    off = None
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
    if m and len(s) > 10:
        sign = 1 if m.group(1) == "+" else -1
        off = sign * (int(m.group(2)) * 60 + int(m.group(3)))
        s = s[:m.start()].strip()
    d = None
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if d is None:
        return None
    if off:
        d = d - timedelta(minutes=off)
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d if DATE_MIN <= d <= DATE_MAX else None


def _decimals(text):
    m = re.search(r"\.(\d+)", str(text or ""))
    return len(m.group(1)) if m else 0


def precision_metres(spec, raw_value, column, lon_text, lat_text):
    """Estimated positional uncertainty radius in metres, on the common scale.

    Resolution order, and each step is a fallback for the previous one failing:
      1. the column NAME, when it states its unit ("...uncertaintyinmeters",
         "positionalaccuracy", "precisionm") or states Flickr's ladder ("acc",
         "geoaccuracy");
      2. the source's declared `precision_kind`;
      3. `spec.unknown_m` when the source HAS a precision column and this row
         left it blank (3,660 of 19,866 iNaturalist rows, 4,437 of 9,099 GBIF
         ones), because a blank accuracy is not evidence of a good fix. It is
         not evidence of a bad one either, so those rows are plotted rather
         than dropped, but they sit at the coarse threshold: no connecting
         lines, no coordinate-based dedup;
      4. the number of decimal places the coordinate is stored to, when the
         source ships no precision column at all (`column=None`). That is a
         real signal - a Commons {{Location}} transcribed from degrees/minutes
         lands at 2 dp, i.e. 1.1 km, and no amount of source metadata makes
         that coordinate better than the digits it was written with.
    Never returns better than the source's floor: a source cannot be more
    precise than its own mechanism.
    """
    n = _norm(column)
    kind = spec.precision_kind
    if any(f in n for f in ("uncertainty", "positionalaccuracy", "posaccuracy",
                            "meters", "metres", "precisionm", "accuracym")):
        kind = "metres"
    elif n in ("acc", "geoaccuracy", "accuracylevel", "flickraccuracy"):
        kind = "level"
    elif "coordinateprecision" in n:
        kind = "degrees"

    val = None
    if raw_value not in (None, "", "NA", "null", "\\N"):
        try:
            val = float(raw_value)
        except (TypeError, ValueError):
            val = None

    est = None
    if val is not None:
        if kind == "level":
            lv = int(round(val))
            est = FLICKR_LEVEL_METRES.get(max(1, min(16, lv)))
        elif kind == "metres":
            est = abs(val)
        elif kind == "degrees":
            est = abs(val) * 111320.0
        else:
            # No unit anywhere. An integer in 1..16 is almost certainly a
            # Flickr-style level (three sources copy that column); anything
            # else is read as metres.
            if float(val).is_integer() and 1 <= val <= 16:
                est = FLICKR_LEVEL_METRES[int(val)]
            else:
                est = abs(val)
    if est is None or est <= 0:
        if column:
            est = spec.unknown_m
        else:
            dp = min(_decimals(lon_text), _decimals(lat_text))
            est = 111320.0 * (10.0 ** -dp) if dp < 6 else spec.floor_m
    return max(float(est), spec.floor_m)


def precision_stated(raw_value):
    """Did the row actually state a precision? Blank cells are reported."""
    return str(raw_value).strip() not in ("", "None", "NA", "na", "null",
                                          "NULL", "\\N", "unknown")


def metres_to_level(m):
    """Back onto Flickr's 1-16 ladder, for lat.fischer's `acc` line gate."""
    for lv in range(16, 0, -1):
        if m <= FLICKR_LEVEL_METRES[lv]:
            return lv
    return 1


_TRUE = {"1", "true", "t", "yes", "y", "obscured", "private"}


def _is_obscured(raw):
    return _norm(raw) in {_norm(v) for v in _TRUE} if raw not in (None, "") else False


# ---------------------------------------------------------------- provenance
# Bot-transferred Commons files put the original photographer's Flickr profile
# in extmetadata.Artist, e.g. "flickr.com/people/34791752@N08". GBIF records
# republished from iNaturalist carry the observation URL. Both are matched
# against the WHOLE raw row rather than a named column, because the column
# these land in differs per source and several of them are free text.
_RE_FLICKR_NSID = re.compile(r"flickr\.com/(?:people|photos)/(\d+@N\d+)", re.I)
_RE_FLICKR_PHOTO = re.compile(r"flickr\.com/photos/[^/\s]+/(\d{6,})", re.I)
_RE_INAT_OBS = re.compile(r"inaturalist\.org/(?:observations|photos)/(\d+)", re.I)


def recover_origin(raw_text):
    """-> (origin_user, origin_ref) or (None, None).

    `origin_user` is a namespaced id for the ORIGINAL photographer where the
    republished row names them; `origin_ref` identifies the original item and
    is the strong dedup key. Recovering the user id is not identity resolution
    across platforms - it is reading a machine-written statement that this row
    IS that Flickr photo.
    """
    user = ref = None
    m = _RE_FLICKR_NSID.search(raw_text)
    if m:
        user = "flickr:" + m.group(1)
    m2 = _RE_FLICKR_PHOTO.search(raw_text)
    if m2:
        ref = "flickr:photo:" + m2.group(1)
    m3 = _RE_INAT_OBS.search(raw_text)
    if m3:
        ref = "inat:obs:" + m3.group(1)
    return user, ref


# ---------------------------------------------------------------- reading
def read_table(path, source, kind="hanoi"):
    """Read one per-source TSV/CSV into rows. Missing file -> ([], report).

    Returns (rows, report). Every row is a dict carrying the
    user/lon/lat/date contract lat.classify needs, plus src, date_kind,
    precision_m, precision_level, acc, coarse, obscured, origin_*.
    """
    spec = SOURCES.get(source) or SourceSpec(source, source, TAKEN)
    rep = {"path": path, "source": source, "kind": kind, "exists": False,
           "rows_read": 0, "kept": 0, "bad_date": 0, "bad_coord": 0,
           "no_user": 0, "columns": {}, "date_column": None,
           "date_kind": spec.date_kind, "obscured": 0, "header": None,
           "date_kind_values": Counter(), "precision_blank": 0,
           "precision_column": None}
    if not os.path.exists(path):
        return [], rep
    rep["exists"] = True

    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        first = f.readline()
        if not first.strip():
            return [], rep
        delim = _sniff_delim(first)
        f.seek(0)
        reader = csv.reader(f, delimiter=delim)
        try:
            head = next(reader)
        except StopIteration:
            return [], rep

        colmap = {}
        if _looks_like_header(head):
            rep["header"] = head
            for i, name in enumerate(head):
                fld = _ALIAS_TO_FIELD.get(_norm(name))
                # First column wins for a field, so "id" does not lose to a
                # later "occurrenceid"; except that a date column naming its
                # own kind beats a generic "date".
                if fld and fld not in colmap:
                    colmap[fld] = i
                elif fld == "date" and any(k in _norm(name)
                                           for k, _ in _DATE_KIND_BY_NAME):
                    colmap["date"] = i
            rows_iter = reader
        else:
            colmap = dict(YFCC_POSITIONS)
            rows_iter = iter([head] + list(reader))
            rep["header"] = None
            if len(head) <= max(YFCC_POSITIONS.values()):
                # No recognisable header AND not the YFCC layout. Guessing
                # which of these columns is a longitude would be inventing
                # data, and reading on would just count every row as a bad
                # coordinate, which reads like a data problem rather than an
                # unreadable file.
                rep["error"] = (f"no header row and only {len(head)} columns: "
                                f"cannot resolve a layout from {head!r}")
                return [], rep

        if "lon" not in colmap or "lat" not in colmap or "date" not in colmap:
            rep["error"] = (f"could not resolve required columns from "
                            f"{head!r}; resolved {sorted(colmap)}")
            return [], rep
        rep["columns"] = {k: (head[v] if rep["header"] else f"col{v}")
                          for k, v in sorted(colmap.items())}

        date_col_name = head[colmap["date"]] if rep["header"] else "col3"
        rep["date_column"] = date_col_name
        date_kind = spec.date_kind
        for frag, dk in _DATE_KIND_BY_NAME:
            if frag in _norm(date_col_name):
                date_kind = dk
                break
        rep["date_kind"] = date_kind

        prec_col = None
        if "precision" in colmap:
            prec_col = (head[colmap["precision"]] if rep["header"]
                        else f"col{colmap['precision']}")
        rep["precision_column"] = prec_col

        def cell(r, field):
            i = colmap.get(field)
            if i is None or i >= len(r):
                return None
            return r[i]

        out = []
        for r in rows_iter:
            if not r or all(not c.strip() for c in r):
                continue
            rep["rows_read"] += 1
            lon_t, lat_t = cell(r, "lon"), cell(r, "lat")
            try:
                lon, lat = float(lon_t), float(lat_t)
            except (TypeError, ValueError):
                rep["bad_coord"] += 1
                continue
            if not (math.isfinite(lon) and math.isfinite(lat)) or \
                    abs(lat) > 90 or abs(lon) > 180 or (lon == 0 and lat == 0):
                rep["bad_coord"] += 1
                continue
            d = parse_date(cell(r, "date"))
            if d is None:
                rep["bad_date"] += 1
                continue
            user = (cell(r, "user") or "").strip()
            if not user:
                # No photographer means no residency and no cap; the row
                # cannot take part in this method at all.
                rep["no_user"] += 1
                continue
            dk = date_kind
            if "date_kind" in colmap:
                v = _norm(cell(r, "date_kind"))
                if v:
                    dk = DATE_KIND_ALIASES.get(v, UNSTATED)
                    rep["date_kind_values"][v] += 1
            pm = precision_metres(spec, cell(r, "precision"), prec_col,
                                  lon_t, lat_t)
            stated = prec_col is not None and precision_stated(cell(r, "precision"))
            if prec_col is not None and not stated:
                rep["precision_blank"] += 1
            raw_text = delim.join(x for x in r if x)
            obsc = _is_obscured(cell(r, "obscured"))
            if source in ("inat", "gbif") and pm >= INAT_OBSCURED_M * 0.5:
                # An iNat/GBIF row whose stated uncertainty is already tens of
                # kilometres is obscured whether or not the flag column came
                # through; treat it as such so a missing column cannot let one
                # onto the map.
                obsc = True
            if obsc:
                rep["obscured"] += 1
            ou, oref = recover_origin(raw_text)
            lv = metres_to_level(pm)
            # A row whose precision was never stated is treated as coarse even
            # though its assigned metre value sits exactly on the threshold:
            # it has not earned a connecting line.
            coarse = pm > PRECISION_COARSE_M or (prec_col is not None
                                                 and not stated)
            out.append({
                "src": source,
                "user": namespaced_user(source, user),
                "raw_user": user,
                "id": (cell(r, "id") or "").strip() or f"{source}:{rep['rows_read']}",
                "lon": lon, "lat": lat, "date": d,
                "date_kind": dk,
                "precision_m": pm, "precision_level": lv,
                "precision_stated": stated,
                # lat.fischer's line gate reads `acc`; giving it the mapped
                # level makes that gate mean the same thing on every source,
                # and a coarse row is forced below the gate so the gate can
                # never be passed by a row we have flagged as unfit for it.
                "acc": min(lv, LINE_GATE_LEVEL - 1) if coarse else lv,
                "coarse": coarse,
                "obscured": obsc,
                "origin_user": ou, "origin_ref": oref,
            })
        rep["kept"] = len(out)
    return out, rep


def load_flickr(overrides_path="data/reloc/overrides.json",
                exclude_path="data/reloc/exclude.json",
                masked_path="data/reloc/masked_ids.json",
                hanoi_path="data/yfcc/hanoi_wide.tsv"):
    """The existing YFCC path, as a source of this merge.

    Read through lat.build so date cleaning, the visual relocations and the
    outside-Hanoi exclusions have exactly one implementation. The vision pass's
    mask is carried as a per-row flag rather than applied here, because it is a
    map mask and not a classification mask - the same split lat/build_fischer.py
    makes.
    """
    import json
    from lat.build import load_hanoi, load_user_history

    if not os.path.exists(hanoi_path):
        return [], [], {"source": "flickr", "exists": False, "kept": 0}
    overrides, exclude, masked = {}, [], set()
    if os.path.exists(overrides_path):
        overrides = {k: tuple(v) for k, v in json.load(open(overrides_path)).items()}
    if os.path.exists(exclude_path):
        exclude = json.load(open(exclude_path))
    if os.path.exists(masked_path):
        masked = set(json.load(open(masked_path)))

    raw = load_hanoi(path=hanoi_path, overrides=overrides, exclude=exclude)
    spec = SOURCES["flickr"]
    rows = []
    for r in raw:
        pm = FLICKR_LEVEL_METRES.get(max(1, min(16, r["acc"])), 3000.0)
        pm = max(pm, spec.floor_m)
        rows.append({
            "src": "flickr", "user": namespaced_user("flickr", r["user"]),
            "raw_user": r["user"],
            "id": r["id"], "lon": r["lon"], "lat": r["lat"], "date": r["date"],
            "date_kind": TAKEN,
            "precision_m": pm, "precision_level": metres_to_level(pm),
            "acc": r["acc"],
            "coarse": pm > PRECISION_COARSE_M, "obscured": False,
            "origin_user": None, "origin_ref": "flickr:photo:" + r["id"],
            "orig_lon": r["orig_lon"], "orig_lat": r["orig_lat"],
            "vision_masked": r["id"] in masked and r["id"] not in overrides,
        })
    hist_raw = load_user_history({r["user"] for r in raw})
    # Every Hanoi photo appears in the worldwide history too, at its
    # pre-relocation coordinate. Dropping those copies is the step
    # lat/build_fischer.py performs for the same reason: left in, the stale
    # coordinate is re-injected and the relocations are void for
    # classification.
    stale = {(r["user"], r["date"].date(), round(r["orig_lon"], 3),
              round(r["orig_lat"], 3)) for r in raw}
    hist = [{"src": "flickr", "user": namespaced_user("flickr", h["user"]),
             "lon": h["lon"],
             "lat": h["lat"], "date": h["date"], "date_kind": TAKEN}
            for h in hist_raw
            if (h["user"], h["date"].date(), round(h["lon"], 3),
                round(h["lat"], 3)) not in stale]
    rep = {"source": "flickr", "exists": True, "kept": len(rows),
           "history_rows": len(hist),
           "history_dropped_stale": len(hist_raw) - len(hist),
           "date_kind": TAKEN, "vision_masked": sum(1 for r in rows
                                                    if r["vision_masked"])}
    return rows, hist, rep


# Filename prefixes that name the same source. Concurrent harvests do not
# agree on a spelling, and `inat_hanoi.tsv` / `inaturalist_hanoi.tsv` both
# turned up in this directory holding the same observations.
SOURCE_KEY_ALIASES = {
    "inaturalist": "inat", "inatobs": "inat", "inaturalistobs": "inat",
    "osmnote": "osmnotes", "notes": "osmnotes", "osmnotes": "osmnotes",
    "commonssql": "commonsdump", "commonsdumps": "commonsdump",
    "wikimediacommons": "commons", "commonsapi": "commons",
    "oam": "openaerialmap",
}


def canonical_source(prefix):
    k = re.sub(r"[^a-z0-9]", "", (prefix or "").lower())
    return SOURCE_KEY_ALIASES.get(k, k)


def _harvest_score(directory, prefix):
    """How complete a harvest is, for choosing between two of the same source.

    The weights are not arbitrary. `inaturalist_hanoi.tsv` (20,899 rows) and
    `inat_hanoi.tsv` (19,866 rows) hold the same observations, but only the
    second ships a `geoprivacy` column, and all 1,033 obscured observations are
    present in the first: of those, 57 carry an uncertainty large enough for
    the fallback heuristic to catch, so choosing the bigger file would have put
    976 randomised coordinates on the map for 1,033 extra rows. So an
    obscuring flag outweighs everything else, then a worldwide history file
    (without which no photographer can ever be red), then the documentation,
    and row count only breaks the remaining ties.
    """
    score = 0
    hanoi = os.path.join(directory, f"{prefix}_hanoi.tsv")
    if os.path.exists(os.path.join(directory, f"{prefix}_history.tsv")):
        score += 40
    if os.path.exists(os.path.join(directory, f"{prefix}_report.md")):
        score += 10
    if os.path.exists(os.path.join(directory, f"{prefix}_hanoi_excluded.tsv")):
        score += 10
    n = 0
    try:
        with open(hanoi, newline="", encoding="utf-8", errors="replace") as f:
            head = f.readline()
            if any(_ALIAS_TO_FIELD.get(_norm(c)) == "obscured"
                   for c in re.split(r"[\t,]", head)):
                score += 100
            for n, _line in enumerate(f, 1):
                pass
    except OSError:
        pass
    return score, n


def discover(directory=MULTI_DIR):
    """What is on disk right now. -> {source: {"hanoi", "history", ...}}

    Sources are named by the filename prefix, so an agent dropping
    `panoramax_hanoi.tsv` is picked up without editing this module; an unknown
    prefix gets a default spec and is reported as unknown. Prefixes are
    canonicalised, and where two of them name the same source only one is
    loaded - loading both would split one photographer into two ids, and
    same-source rows are deliberately never deduplicated against each other.
    """
    by_prefix = defaultdict(dict)
    for path in sorted(glob.glob(os.path.join(directory, "*"))):
        base = os.path.basename(path)
        m = re.match(r"([A-Za-z0-9_]+?)_(hanoi|history)\.(tsv|csv)$", base)
        if m:
            by_prefix[m.group(1)][m.group(2).lower()] = path

    grouped = defaultdict(list)
    for prefix in sorted(by_prefix):
        grouped[canonical_source(prefix)].append(prefix)

    found = {}
    for src, prefixes in grouped.items():
        best = max(prefixes, key=lambda p: _harvest_score(directory, p))
        entry = dict(by_prefix[best])
        entry["prefix"] = best
        if len(prefixes) > 1:
            entry["also_on_disk"] = [
                {"prefix": p, "score": _harvest_score(directory, p)[0],
                 "rows": _harvest_score(directory, p)[1]}
                for p in prefixes if p != best]
        found[src] = entry
    return found


def verify_excluded(directory=MULTI_DIR):
    """Re-read any `<src>_hanoi_excluded.tsv` and check we would drop it too.

    The iNaturalist harvest ships its own rejects (1,033 rows, all
    geoprivacy=obscured). They are a free adversarial test of the precision
    filter: every row another agent judged unmappable must also be unmappable
    here. If a row in that file WOULD reach our map, either their reason or our
    filter is wrong, and this reports it rather than assuming agreement.
    """
    out = {}
    for path in sorted(glob.glob(os.path.join(directory, "*_hanoi_excluded.tsv"))):
        src = os.path.basename(path).split("_")[0].lower()
        rows, rep = read_table(path, src, "excluded")
        kept, dropped = filter_precision(rows)
        reasons = Counter()
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            rd = csv.reader(f, delimiter="\t")
            head = next(rd, [])
            if "exclude_reason" in head:
                i = head.index("exclude_reason")
                for r in rd:
                    if len(r) > i:
                        reasons[r[i]] += 1
        out[src] = {"rows_readable": len(rows), "would_still_map": len(kept),
                    "dropped_by_us": {f"{a}/{b}": n for (a, b), n in dropped.items()},
                    "their_reasons": dict(reasons),
                    "agrees": len(kept) == 0}
    return out


def enabled_sources():
    """Source keys that are on by default. See each spec's `excluded_because`."""
    return {k for k, sp in SOURCES.items() if sp.default_on}


def load_all(directory=MULTI_DIR, include_flickr=True, sources=None,
             all_sources=False):
    """-> (hanoi_rows, history_rows, reports)

    `sources` restricts to an explicit set. With neither `sources` nor
    `all_sources`, a source whose spec is `default_on=False` is skipped and
    reported as skipped, not silently ignored: GBIF and Panoramax are both
    present on disk and both measured not worth mapping.

    Tolerates every input being absent: with an empty data/multi and no YFCC
    table this returns empty lists and a report saying so, rather than raising.
    """
    reports = []
    hanoi, hist = [], []

    def wanted(src):
        if sources is not None:
            return src in sources
        if all_sources:
            return True
        sp = SOURCES.get(src)
        return sp.default_on if sp else True

    if include_flickr and wanted("flickr"):
        fr, fh, rep = load_flickr()
        hanoi += fr
        hist += fh
        reports.append(rep)
    for src, paths in sorted(discover(directory).items()):
        if src == "flickr":
            continue
        if not wanted(src):
            sp = SOURCES.get(src)
            reports.append({"source": src, "exists": True, "skipped": True,
                            "kept": 0, "history_rows": 0,
                            "excluded_because": sp.excluded_because if sp else ""})
            continue
        rows, rep = read_table(paths.get("hanoi", os.path.join(directory, f"{src}_hanoi.tsv")),
                               src, "hanoi")
        hanoi += rows
        hrows, hrep = read_table(paths.get("history",
                                           os.path.join(directory, f"{src}_history.tsv")),
                                 src, "history")
        hist += hrows
        rep["history_rows"] = hrep["kept"]
        rep["history_report"] = hrep
        rep["known_source"] = src in SOURCES
        rep["prefix"] = paths.get("prefix", src)
        if paths.get("also_on_disk"):
            rep["also_on_disk"] = paths["also_on_disk"]
        reports.append(rep)
    # The sibling reports document the column layouts; note which are readable
    # so a merge run says whether it was flying blind.
    docs = sorted(os.path.basename(p)
                  for p in glob.glob(os.path.join(directory, "*_report.md")))
    return hanoi, hist, {"sources": reports, "report_docs": docs,
                         "dir": directory}


# ---------------------------------------------------------------- filtering
# A merged map is a 2004-2026 composite, not the 2010 snapshot Fischer made:
# YFCC100M stops in 2014 while iNaturalist is overwhelmingly 2020s (17,593 of
# 19,866 harvested rows) and Commons straddles both (10,836 in the 2010s,
# 6,479 in the 2020s). The window is therefore a first-class option, so a
# like-for-like 2004-2014 merge can be rendered next to the all-time one, and
# the all-time default is disclosed rather than assumed.
YFCC_WINDOW = (datetime(2004, 1, 1), datetime(2015, 6, 1))


def filter_window(rows, window):
    """Keep rows whose date falls in `window` = (from, to), either end None."""
    if not window:
        return list(rows), Counter()
    lo, hi = window
    kept, dropped = [], Counter()
    for r in rows:
        d = r["date"]
        if (lo is not None and d < lo) or (hi is not None and d > hi):
            dropped[r["src"]] += 1
            continue
        kept.append(r)
    return kept, dropped


def drop_obscured(rows):
    """Remove deliberately falsified coordinates from EVERYTHING.

    Coarse and obscured are different in kind. A city-level geotag is a bad
    measurement of a real position, and the existing build keeps such rows for
    classification for exactly that reason: it is still evidence the
    photographer was in Hanoi. An obscured iNaturalist coordinate is not a bad
    measurement, it is a random point in a 22 km cell, so it is not evidence of
    presence anywhere - it can invent a Hanoi visit as easily as hide one - and
    it is removed before residency as well as before rendering.
    """
    kept, dropped = [], Counter()
    for r in rows:
        if r.get("obscured"):
            dropped[(r["src"], "obscured")] += 1
            continue
        kept.append(r)
    assert not any(r.get("obscured") for r in kept), \
        "obscured coordinates are randomised inside ~22 km and must never be used"
    if any(r.get("obscured") for r in kept):          # survives python -O
        raise RuntimeError("obscured coordinate survived drop_obscured")
    return kept, dropped


def filter_precision(rows, drop_m=PRECISION_DROP_M):
    """The MAP filter: drop rows too coarse for a 24 km / 3.95 m-per-px canvas.

    A position uncertain by more than `drop_m` (5 km) could be anywhere in a
    fifth of the canvas, so the mark asserts a place nobody stood. Rows between
    PRECISION_COARSE_M and drop_m are kept, flagged `coarse`, and excluded from
    connecting lines by lat.fischer's existing accuracy gate.

    Obscured rows are dropped here too, so this function is safe to call on
    unfiltered input, and the post-condition is a real check rather than a
    comment: no choice of `drop_m` may ever let an obscured coordinate through.
    """
    kept, dropped = drop_obscured(rows)
    out = []
    for r in kept:
        if r.get("precision_m", 0.0) > drop_m:
            dropped[(r["src"], "too_coarse")] += 1
            continue
        out.append(r)
    assert not any(r.get("obscured") for r in out), \
        "obscured coordinates are randomised inside ~22 km and must never be mapped"
    return out, dropped


def _cap_key(row):
    """Stable pseudo-random order within one photographer's set.

    Taking the first N by date would keep a photographer's earliest outing and
    drop everything after it, which biases the survivors both in time and,
    because people shoot in bursts in one place, in space. Hashing (user, id)
    gives a subsample that keeps the shape of their set, and the same rows
    survive on every run and at every larger cap value (the hash order is a
    total order, so cap=200 is a superset of cap=100).
    """
    return hashlib.sha1(f"{row['user']}|{row.get('id','')}".encode()).hexdigest()


def cap_per_user(rows, cap):
    """Keep at most `cap` rows per namespaced photographer. cap=None -> no cap.

    Applied to the MAP set only. Capping the residency input would truncate the
    spans the whole classification is built on: drop a photographer's last
    photo and a 31-day span becomes 3 days.
    """
    if not cap:
        return list(rows), Counter()
    by_user = defaultdict(list)
    for r in rows:
        by_user[r["user"]].append(r)
    kept, removed = [], Counter()
    for user, rs in by_user.items():
        if len(rs) <= cap:
            kept += rs
            continue
        rs = sorted(rs, key=_cap_key)
        kept += rs[:cap]
        removed[rs[0]["src"]] += len(rs) - cap
    return kept, removed


# ---------------------------------------------------------------- dedup
DEDUP_COORD_DP = 4          # ~11 m at this latitude


def _coord_day_key(r, dp=DEDUP_COORD_DP):
    return (round(r["lon"], dp), round(r["lat"], dp), r["date"].date())


def dedup(rows, coord_dp=DEDUP_COORD_DP, priority=None):
    """Remove copies of the same photograph arriving by different routes.

    Two keys, in order of how much they prove:

    1. A recovered original reference (`origin_ref`): a GBIF record naming an
       iNaturalist observation URL, or a Commons file whose Artist field names
       a Flickr photo. That is the republisher stating the identity, so every
       lower-priority copy in the group goes.

    2. Coordinate at 4 dp (~11 m) plus calendar day. This is circumstantial and
       cannot be applied inside one source - a photographer legitimately takes
       twenty photos at one spot on one day - so it only ever removes rows
       across sources. Even across sources it is only run pairwise-matched:
       in a cell holding 5 iNaturalist rows and 1 GBIF row, one row is a
       duplicate, not five, so at most `n_winner` rows are removed from each
       lower-priority source. Judgement call; the alternative (drop the whole
       lower-priority side) removed real distinct records in a test on
       synthetic clusters.

    Never compares a row with itself and never merges photographers.
    -> (kept, {(kept_src, dropped_src): n}, {"by_key": {...}})
    """
    priority = priority or SOURCE_PRIORITY
    pri = {s: i for i, s in enumerate(priority)}

    def p(src):
        return pri.get(src, len(priority))

    drop = set()
    pairs = Counter()
    by_key = Counter()

    groups = defaultdict(list)
    for i, r in enumerate(rows):
        if r.get("origin_ref"):
            groups[("ref", r["origin_ref"])].append(i)
    for key, idxs in groups.items():
        srcs = {rows[i]["src"] for i in idxs}
        if len(srcs) < 2:
            continue
        win = min(srcs, key=p)
        for i in idxs:
            if rows[i]["src"] != win:
                drop.add(i)
                pairs[(win, rows[i]["src"])] += 1
                by_key["origin_ref"] += 1

    groups = defaultdict(list)
    for i, r in enumerate(rows):
        if i in drop:
            continue
        # A coordinate uncertain by more than a kilometre cannot establish that
        # two rows are the same photograph: Flickr city pins put hundreds of
        # unrelated photos on 105.85, 21.03, and a 2-decimal Commons
        # coordinate lands on the same value. Coarse rows can still be
        # deduplicated by a recovered original reference, above.
        if r.get("coarse"):
            continue
        groups[_coord_day_key(r, coord_dp)].append(i)
    for key, idxs in groups.items():
        by_src = defaultdict(list)
        for i in idxs:
            by_src[rows[i]["src"]].append(i)
        if len(by_src) < 2:
            continue
        win = min(by_src, key=p)
        n_win = len(by_src[win])
        for src, ids in by_src.items():
            if src == win:
                continue
            # deterministic: drop the highest ids by the cap hash order
            victims = sorted(ids, key=lambda i: _cap_key(rows[i]))[:min(len(ids), n_win)]
            for i in victims:
                drop.add(i)
                pairs[(win, src)] += 1
                by_key["coord_day"] += 1

    kept = [r for i, r in enumerate(rows) if i not in drop]
    return kept, pairs, dict(by_key)


# ---------------------------------------------------------------- classify
def residency_records(hanoi_rows, history_rows, date_kinds=RESIDENCY_DATE_KINDS):
    """The record set handed to lat.classify, restricted by date kind.

    Only user/lon/lat/date are passed through, which is lat.classify's whole
    contract; everything else about a row is a rendering concern.
    """
    kinds = set(date_kinds) if date_kinds else None
    out = []
    for r in list(hanoi_rows) + list(history_rows):
        if kinds is not None and r.get("date_kind", TAKEN) not in kinds:
            continue
        out.append({"user": r["user"], "lon": r["lon"], "lat": r["lat"],
                    "date": r["date"]})
    return out


def classify_multi(hanoi_rows, history_rows, bounds=HANOI_BOUNDS,
                   date_kinds=RESIDENCY_DATE_KINDS):
    """-> (labels {user: label}, info) using lat.classify unchanged.

    A photographer with no residency-eligible row (an upload-dated source, or
    a source whose only rows were dropped as obscured) cannot be tested and is
    labelled UNKNOWN. That is not a fudge: yellow already means "no span of a
    month could be established anywhere", and for an upload-only account that
    is precisely true.
    """
    s_, w_, n_, e_ = bounds

    def in_city(lon, lat):
        return w_ <= lon <= e_ and s_ <= lat <= n_

    recs = residency_records(hanoi_rows, history_rows, date_kinds)
    labels_full = classify_users(recs, in_city, city_bounds=bounds)
    labels = {u: v[0] for u, v in labels_full.items()}

    all_users = {r["user"] for r in hanoi_rows}
    untested = sorted(all_users - set(labels))
    for u in untested:
        labels[u] = UNKNOWN
    counts = Counter(labels[u] for u in all_users)
    info = {"users_total": len(all_users),
            "users_classified": len(all_users) - len(untested),
            "users_untested_no_eligible_date": len(untested),
            "residency_date_kinds": sorted(date_kinds) if date_kinds else "all",
            "residency_records": len(recs),
            "counts": dict(counts)}
    return labels, info


# ---------------------------------------------------------------- pipeline
def merge(directory=MULTI_DIR, cap=None, include_flickr=True, sources=None,
          drop_m=PRECISION_DROP_M, date_kinds=RESIDENCY_DATE_KINDS,
          apply_vision_mask=True, window=None, all_sources=False,
          verbose=True):
    """Load, filter, dedup, classify, cap. -> dict of everything downstream needs.

    Order matters and is deliberate:
      drop obscured -> dedup -> classify -> map precision filter -> cap
    Obscured first, because a randomised coordinate must not be allowed to
    match a real one in the dedup or to assert a presence in classification.
    Dedup before classify so one photograph reaching us twice cannot count
    twice. The precision filter and the vision mask come AFTER classification,
    both for the same reason the single-source build masks after classifying: a
    coarse geotag is still evidence the photographer was in Hanoi, and dropping
    it before the span test would silently reclassify people. Cap last, and on
    the map set only, so the cap changes the picture and never the labels.
    """
    hanoi, hist, load_rep = load_all(directory, include_flickr, sources,
                                     all_sources)
    if verbose:
        for rep in load_rep["sources"]:
            if rep.get("skipped"):
                print(f"  {rep['source']:12s} present but NOT merged: "
                      f"{rep.get('excluded_because', '')[:120]}...")
                continue
            if not rep.get("exists"):
                print(f"  {rep['source']:12s} absent")
                continue
            extra = ""
            if rep.get("error"):
                extra = "  ERROR " + rep["error"]
            print(f"  {rep['source']:12s} {rep['kept']:>7,} rows  "
                  f"history {rep.get('history_rows', 0):>8,}  "
                  f"date_kind={rep.get('date_kind')}"
                  f"{'  obscured ' + str(rep['obscured']) if rep.get('obscured') else ''}"
                  f"{extra}")
            if rep.get("columns"):
                print(f"               columns {rep['columns']}")
            for other in rep.get("also_on_disk", []):
                print(f"               ALSO on disk as {other['prefix']}_hanoi.tsv "
                      f"({other['rows']:,} rows, completeness score "
                      f"{other['score']} vs {rep['prefix']}): not loaded, see "
                      f"_harvest_score")
            if rep.get("date_kind_values"):
                print(f"               date_kind column {dict(rep['date_kind_values'])}"
                      f" -> {dict(Counter(DATE_KIND_ALIASES.get(k, UNSTATED) for k in rep['date_kind_values'].elements()))}")
            if rep.get("precision_blank"):
                sp = SOURCES.get(rep["source"])
                unk = sp.unknown_m if sp else PRECISION_COARSE_M
                print(f"               {rep['precision_blank']:,} rows state no "
                      f"precision in column {rep['precision_column']!r}: "
                      f"treated as {unk:.0f} m, plotted but flagged coarse")
            if rep.get("bad_date"):
                print(f"               {rep['bad_date']:,} rows dropped on date "
                      f"(outside {DATE_MIN.date()}..{DATE_MAX.date()}, or unparseable)")
        if load_rep["report_docs"]:
            print(f"  source docs present: {', '.join(load_rep['report_docs'])}")
        else:
            print("  no *_report.md alongside the data: column layouts were "
                  "resolved by header alias only")

    n_in = len(hanoi)
    hanoi, win_dropped = filter_window(hanoi, window)
    hist, win_h = filter_window(hist, window)
    win_dropped.update(win_h)
    if verbose and window:
        lo, hi = window
        print(f"  date window {lo and lo.date()}..{hi and hi.date()}: dropped "
              f"{sum(win_dropped.values()):,} rows {dict(win_dropped)}")

    residency_rows, obsc = drop_obscured(hanoi)
    hist, obsc_h = drop_obscured(hist)
    obsc.update(obsc_h)
    if verbose and obsc:
        for (src, why), n in sorted(obsc.items()):
            print(f"  dropped {n:,} {src} rows ({why}: randomised coordinate)")

    residency_rows, dedup_pairs, dedup_kinds = dedup(residency_rows)
    if verbose and dedup_pairs:
        for (a, b), n in sorted(dedup_pairs.items(), key=lambda kv: -kv[1]):
            print(f"  dedup: {n:,} {b} rows duplicated {a} rows")

    labels, cinfo = classify_multi(residency_rows, hist, date_kinds=date_kinds)

    map_rows, prec_dropped = filter_precision(residency_rows, drop_m)
    if verbose and prec_dropped:
        for (src, why), n in sorted(prec_dropped.items()):
            print(f"  map filter: dropped {n:,} {src} rows ({why}, "
                  f"> {drop_m:.0f} m; kept for classification)")
    # The vision pass's coordinate mask applies to the Flickr rows only: it was
    # adjudicated against Flickr photographs. Kept for classification, as in
    # lat/build_fischer.py.
    if apply_vision_mask:
        map_rows = [r for r in map_rows if not r.get("vision_masked")]

    capped, cap_removed = cap_per_user(map_rows, cap)
    if verbose and cap:
        print(f"  cap {cap}/photographer: removed {sum(cap_removed.values()):,} "
              f"of {len(map_rows):,} map rows")

    return {"rows": capped, "map_rows_uncapped": map_rows,
            "residency_rows": residency_rows, "history": hist,
            "labels": labels, "classify": cinfo, "load": load_rep,
            "rows_loaded": n_in,
            "window": [w and w.isoformat() for w in window] if window else None,
            "window_dropped": dict(win_dropped),
            "obscured_dropped": {f"{s}/{w}": n for (s, w), n in obsc.items()},
            "precision_dropped": {f"{s}/{w}": n for (s, w), n in prec_dropped.items()},
            "dedup_pairs": {f"{a}->{b}": n for (a, b), n in dedup_pairs.items()},
            "dedup_by_key": dedup_kinds,
            "cap": cap,
            "cap_removed": dict(cap_removed)}


def cap_sweep(map_rows, caps=(None, 2000, 1000, 500, 200, 100, 50), frame=None):
    """Point counts per source at several caps, so the trade-off is reportable.

    `frame` (an EquirectFrame), when given, counts only rows that land on the
    canvas, which is the number that actually changes the picture.
    """
    rows = map_rows
    if frame is not None:
        rows = [r for r in rows if frame.contains(r["lon"], r["lat"])]
    out = []
    for cap in caps:
        kept, _ = cap_per_user(rows, cap)
        by_src = Counter(r["src"] for r in kept)
        users = len({r["user"] for r in kept})
        counts = Counter(r["user"] for r in kept)
        top = counts.most_common(5)
        n = len(kept) or 1
        # The share the biggest few accounts hold is the number that says
        # whether the reader is looking at a city or at somebody's album, so it
        # is reported at every cap value rather than once.
        out.append({"cap": cap, "points": len(kept), "photographers": users,
                    "by_source": dict(by_src),
                    "top3_share": round(sum(c for _u, c in top[:3]) / n, 4),
                    "top5_share": round(sum(c for _u, c in top[:5]) / n, 4),
                    "top5": [[u, c] for u, c in top]})
    return out


def source_summary(rows, labels):
    """Per-source, per-class point counts for stats.json."""
    out = {}
    for r in rows:
        s = out.setdefault(r["src"], {"points": 0, "photographers": set(),
                                      LOCAL: 0, TOURIST: 0, UNKNOWN: 0,
                                      "coarse": 0, "date_kinds": Counter()})
        s["points"] += 1
        s["photographers"].add(r["user"])
        s[labels.get(r["user"], UNKNOWN)] += 1
        s["coarse"] += 1 if r.get("coarse") else 0
        s["date_kinds"][r.get("date_kind", TAKEN)] += 1
    for s in out.values():
        s["photographers"] = len(s["photographers"])
        s["date_kinds"] = dict(s["date_kinds"])
    return out
