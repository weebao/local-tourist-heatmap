"""Spherical Web Mercator projection + the square framing convention used for
Eric Fischer's city maps."""
import math

R_MAJOR = 6378137.0


def lonlat_to_merc(lon, lat):
    """Lon/lat degrees -> Web Mercator metres (EPSG:3857)."""
    x = math.radians(lon) * R_MAJOR
    lat = max(min(lat, 89.5), -89.5)
    y = R_MAJOR * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def merc_to_lonlat(x, y):
    lon = math.degrees(x / R_MAJOR)
    lat = math.degrees(2 * math.atan(math.exp(y / R_MAJOR)) - math.pi / 2)
    return lon, lat


class SquareFrame:
    """A square Web-Mercator viewport of `size` pixels centred on a lon/lat.

    Fischer framed each city as a square, so a square Mercator extent keeps the
    aspect ratio honest: north-south and east-west scale identically in 3857.
    """

    def __init__(self, center_lon, center_lat, span_m, size):
        self.cx, self.cy = lonlat_to_merc(center_lon, center_lat)
        self.span_m = float(span_m)
        self.size = int(size)
        self.scale = self.size / self.span_m  # px per metre
        self.x0 = self.cx - self.span_m / 2
        self.y1 = self.cy + self.span_m / 2  # top edge (mercator y grows north)

    def to_px(self, lon, lat):
        """-> (float px_x, float px_y) with origin at top-left."""
        x, y = lonlat_to_merc(lon, lat)
        return (x - self.x0) * self.scale, (self.y1 - y) * self.scale

    def bounds_lonlat(self):
        w, s = merc_to_lonlat(self.x0, self.y1 - self.span_m)
        e, n = merc_to_lonlat(self.x0 + self.span_m, self.y1)
        return w, s, e, n

    def zoom_for_tiles(self, tile_px=256):
        """Web Mercator zoom whose native pixel scale is closest to this frame."""
        world_px = self.size * (2 * math.pi * R_MAJOR) / self.span_m
        return math.log2(world_px / tile_px)

    def __repr__(self):
        w, s, e, n = self.bounds_lonlat()
        return (f"SquareFrame({self.size}px, span={self.span_m/1000:.1f}km, "
                f"bounds=({w:.4f},{s:.4f},{e:.4f},{n:.4f}), "
                f"z={self.zoom_for_tiles():.2f})")
