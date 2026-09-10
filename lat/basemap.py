"""Rasterise Overpass road/water geometry into a coverage raster.

Fischer's basemap is a fine web of pale yellow street lines on white. We build a
float coverage raster in [0,1] per weight class by drawing each class into a
supersampled bitmap and box-downsampling it, which gives clean antialiasing
without a drawing library beyond Pillow.
"""
import gzip
import json
import numpy as np
from PIL import Image, ImageDraw

# width in final-image pixels, by OSM highway class
ROAD_CLASSES = {
    "motorway": 1.7, "motorway_link": 1.1, "trunk": 1.7, "trunk_link": 1.1,
    "primary": 1.5, "primary_link": 1.0,
    "secondary": 1.2, "secondary_link": 0.9,
    "tertiary": 1.0, "tertiary_link": 0.8,
    "residential": 0.8, "unclassified": 0.8, "living_street": 0.8,
    "service": 0.6, "pedestrian": 0.7,
    "track": 0.6, "path": 0.5, "footway": 0.5, "cycleway": 0.5,
    "steps": 0.5, "bridleway": 0.5, "road": 0.7, "busway": 0.9,
}
RIVER_WIDTH = {"river": 2.4, "canal": 1.6, "stream": 0.8, "ditch": 0.6,
               "drain": 0.6, "riverbank": 2.4}


def load_overpass(path):
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as f:
        doc = json.load(f)
    nodes, ways = {}, []
    for el in doc["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way":
            ways.append(el)
    return nodes, ways


# Pedestrian paths and driveways are mapped far more exhaustively in today's
# OSM than in the 2010-era basemap this style was designed against; including
# them floods a city as densely mapped as Hanoi. Excluded by default.
DEFAULT_EXCLUDE = {"footway", "path", "steps", "cycleway", "bridleway",
                   "corridor", "platform"}


def build_layers(nodes, ways, frame, ss=4, exclude=None):
    """-> dict name -> HxW float coverage in [0,1] (H=W=frame.size)."""
    exclude = DEFAULT_EXCLUDE if exclude is None else set(exclude)
    S = frame.size * ss
    # group ways by the stroke width they should get
    strokes = {}   # width_px -> list of pixel polylines
    waters = []    # polygons to fill

    for w in ways:
        tags = w.get("tags") or {}
        refs = w.get("nodes") or []
        if len(refs) < 2:
            continue
        pts = []
        for r in refs:
            nd = nodes.get(r)
            if nd is None:
                continue
            x, y = frame.to_px(nd[0], nd[1])
            pts.append((x * ss, y * ss))
        if len(pts) < 2:
            continue

        if tags.get("natural") == "water" or tags.get("landuse") == "reservoir" \
                or tags.get("waterway") == "riverbank":
            if refs[0] == refs[-1] and len(pts) >= 3:
                waters.append(pts)
                continue
        if "highway" in tags:
            if tags["highway"] in exclude:
                continue
            wid = ROAD_CLASSES.get(tags["highway"], 0.6)
        elif "waterway" in tags:
            wid = RIVER_WIDTH.get(tags["waterway"], 0.8)
        else:
            continue
        strokes.setdefault(round(wid, 2), []).append(pts)

    layers = {}
    # water bodies, filled
    if waters:
        img = Image.new("1", (S, S), 0)
        d = ImageDraw.Draw(img)
        for poly in waters:
            d.polygon(poly, fill=1)
        layers["water_fill"] = _down(img, ss)
    # line strokes, one bitmap per width so widths stay crisp
    for wid, polys in strokes.items():
        img = Image.new("1", (S, S), 0)
        d = ImageDraw.Draw(img)
        lw = max(1, int(round(wid * ss)))
        for pts in polys:
            d.line(pts, fill=1, width=lw)
        layers[f"line_{wid}"] = _down(img, ss)
    return layers


def _down(img, ss):
    a = np.asarray(img, dtype=np.float32)
    S = a.shape[0]
    n = S // ss
    return a.reshape(n, ss, n, ss).mean(axis=(1, 3))


# Fitted against the London reference's road-ink distribution (mean, p50, p90,
# p99, max) over its sparse outer margin: mean relative error 5.7%.
ROAD_ALPHA = 0.09
ROAD_SIGMA = 1.1


def combine(layers, road_weight=1.0, water_weight=1.35, sigma=ROAD_SIGMA):
    """Sum coverage layers into one raster (values may exceed 1 where roads
    overlap; the ink mapping clamps later).

    `sigma` softens the strokes. Without it the ink distribution is far more
    skewed than the original's (p90/p50 4.7 vs 2.5) - real pen strokes spread
    ink into neighbouring pixels.
    """
    total = None
    for name, lay in layers.items():
        wgt = water_weight if name == "water_fill" else road_weight
        total = lay * wgt if total is None else total + lay * wgt
    if total is None:
        return None
    if sigma > 0:
        from scipy import ndimage
        total = ndimage.gaussian_filter(total, sigma)
    return total
