"""Renderer for the Locals-and-Tourists style.

The compositing model was reverse-engineered from Fischer's published London
map (data/ref_london.jpg) and validated against it: 99.2% of that image's
pixels invert cleanly through it, and it reproduces the map's landmark colours
(#ffffcc roads, #000039 dense blue-over-red, #8d80c4 mid blue) to within JPEG
noise.

It is ordinary subtractive ink mixing on a WHITE page:

    blue   ink removes R and G      ink vector (1, 1, 0)
    red    ink removes G and B                 (0, 1, 1)
    yellow ink removes B                       (0, 0, 1)

    S = sum_c  a_c * ink_c          out = 255 * (1 - clamp(S, 0, 1))

Because the sum is taken before clamping, dense blue *and* red together drive
every channel down and the pixel goes dark navy - which is exactly what the
original does, and what plain alpha-over cannot do.
"""
import numpy as np

LOCAL, TOURIST, UNKNOWN = "local", "tourist", "unknown"

# Ink vectors, in channel-removal space.
INK = {
    LOCAL:   np.array([1.0, 1.0, 0.0]),  # blue
    TOURIST: np.array([0.0, 1.0, 1.0]),  # red
    UNKNOWN: np.array([0.0, 0.0, 1.0]),  # yellow
}
ROAD_INK = np.array([0.0, 0.0, 1.0])     # legacy: roads as pure yellow ink

# Measured from the reference's sparse outer margin, where road lines are not
# contaminated by photo dots: a road centre pixel sits around #f6f6ea, the
# darkest around #f5f5cb. So a road removes a little R and G and a lot of B -
# a pale khaki, which no combination of the three point inks can make. The
# basemap is therefore a layer *under* the points, and points multiply over it.
ROAD_DROP = np.array([8.0, 8.0, 30.0])   # 8-bit drop per unit road coverage
ROAD_COVER_CAP = 1.8


def _stamp(acc, xs, ys, radius, weight=1.0):
    """Additively stamp points into `acc` with a small round brush.

    radius 0 -> single pixel. Fractional pixel positions are bilinearly
    distributed so fine linear features (a street, a lakeshore) stay smooth
    instead of stair-stepping.
    """
    h, w = acc.shape
    if radius <= 0:
        x0 = np.floor(xs).astype(np.int64); y0 = np.floor(ys).astype(np.int64)
        fx = xs - x0; fy = ys - y0
        for dx, dy, wt in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                           (0, 1, (1-fx)*fy),     (1, 1, fx*fy)):
            xi = x0 + dx; yi = y0 + dy
            m = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
            np.add.at(acc, (yi[m], xi[m]), (wt[m] * weight))
        return

    r = int(np.ceil(radius))
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            d = np.hypot(dx, dy)
            if d > radius + 0.5:
                continue
            # soft edge over the outermost half pixel
            wt = weight * min(1.0, max(0.0, radius + 0.5 - d))
            xi = np.round(xs).astype(np.int64) + dx
            yi = np.round(ys).astype(np.int64) + dy
            m = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
            np.add.at(acc, (yi[m], xi[m]), wt)


def density_to_ink(acc, k, gamma, cap=1.0):
    """Map an additive count raster to ink coverage in [0, cap].

    a = (k * count) ** gamma, clamped. gamma < 1 compresses the dynamic range so
    a lone photograph still marks the page while dense districts saturate.
    """
    with np.errstate(invalid="ignore"):
        a = np.power(np.maximum(acc, 0.0) * k, gamma)
    return np.clip(np.nan_to_num(a), 0.0, cap)


class Canvas:
    def __init__(self, size):
        self.size = size
        self.S = np.zeros((size, size, 3), dtype=np.float32)

    def add_ink(self, coverage, ink):
        """coverage: HxW float in [0,1]; ink: 3-vector of channel removal."""
        for ch in range(3):
            if ink[ch]:
                self.S[:, :, ch] += coverage * ink[ch]

    def to_image(self, base=None):
        """base: optional HxWx3 float 0-255 basemap. Points MULTIPLY over it,
        so dense blue on a road goes to (0, 0, road_blue) exactly as in the
        original, and a white page reduces to the pure-ink case."""
        from PIL import Image
        b = 255.0 if base is None else base
        out = b * (1.0 - np.clip(self.S, 0.0, 1.0))
        return Image.fromarray(np.round(np.clip(out, 0, 255)).astype(np.uint8), "RGB")


def basemap_rgb(roads, size, drop=ROAD_DROP, cap=ROAD_COVER_CAP):
    """Road coverage raster -> white page with pale khaki streets."""
    base = np.full((size, size, 3), 255.0, dtype=np.float32)
    if roads is None:
        return base
    cov = np.clip(roads, 0.0, cap)[:, :, None]
    return np.clip(base - cov * drop[None, None, :], 0, 255)


def render(frame, points, roads=None, *, dot_radius=0.0, point_sigma=0.6,
           k=1.0, gamma=0.55, class_scale=None,
           road_drop=ROAD_DROP, road_cap=ROAD_COVER_CAP):
    """points: dict label -> (xs, ys) arrays in pixel space.
    roads: optional HxW float raster of road coverage.
    """
    c = Canvas(frame.size)
    base = basemap_rgb(roads, frame.size, road_drop, road_cap)
    class_scale = class_scale or {}
    # Draw densest class last only matters for clamping; sum is order-free.
    for label in (UNKNOWN, TOURIST, LOCAL):
        if label not in points:
            continue
        xs, ys = points[label]
        if len(xs) == 0:
            continue
        acc = np.zeros((frame.size, frame.size), dtype=np.float32)
        _stamp(acc, np.asarray(xs, float), np.asarray(ys, float), dot_radius)
        if point_sigma > 0:
            # Round the marks off. Stamping a disc brush with a linear falloff
            # leaves plus-shaped dots (only the 4 cardinal neighbours catch any
            # weight); blurring single-pixel hits gives a proper round dot and
            # keeps sub-pixel positions honest.
            from scipy import ndimage
            acc = ndimage.gaussian_filter(acc, point_sigma, mode="constant")
        cov = density_to_ink(acc, k * class_scale.get(label, 1.0), gamma)
        c.add_ink(cov, INK[label])
    return c.to_image(base)
