"""Resolve Hanoi place names to coordinates via Nominatim (OSM), with an
on-disk cache. Used to turn the visual-identification pass's free-text answers
into coordinates without hand-asserting any lat/lon.
"""
import json, os, time
import requests

CACHE = "data/reloc/geocode_cache.json"
UA = "hanoi-locals-tourists/1.0 (contact: baochidangg@gmail.com)"
# Hanoi viewbox: left, top, right, bottom
VIEWBOX = "105.55,21.30,106.15,20.80"


def _load():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    return {}


def _save(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(c, open(CACHE, "w"), indent=1, ensure_ascii=False)


def _variants(name):
    """Progressively simpler queries. The vision pass returns rich labels like
    "Hoa Lo Prison (Maison Centrale), 1 Hoa Lo, Hanoi"; Nominatim resolves the
    plain landmark name but not the decorated one, so try the decorations off.
    """
    import re
    out, seen = [], set()

    def add(q):
        q = re.sub(r"\s+", " ", q).strip(" ,;")
        if q and q.lower() not in seen:
            seen.add(q.lower()); out.append(q)

    add(name)
    bare = re.sub(r"\([^)]*\)", " ", name)      # drop parentheticals
    add(bare)
    parts = [p.strip() for p in bare.split(",") if p.strip()]
    if parts:
        add(parts[0])                            # leading landmark alone
        # a leading house number is noise for a landmark lookup
        add(re.sub(r"^\s*\d+[A-Za-z]?\s+", "", parts[0]))
        if len(parts) >= 2:
            add(", ".join(parts[:2]))
            add(parts[1])                        # e.g. the street, not the shop
    return out


def geocode(name, cache=None, pause=1.1):
    """-> (lon, lat, display_name) or None. Bounded to the Hanoi viewbox."""
    cache = _load() if cache is None else cache
    key = name.strip().lower()
    if key in cache:
        v = cache[key]
        return tuple(v) if v else None

    val = None
    for cand in _variants(name):
        low = cand.lower()
        q = cand if ("hanoi" in low or "hà nội" in low or "vietnam" in low) \
            else f"{cand}, Hanoi, Vietnam"
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"q": q, "format": "json", "limit": 1,
                                     "viewbox": VIEWBOX, "bounded": 1},
                             headers={"User-Agent": UA}, timeout=25)
            time.sleep(pause)   # Nominatim asks for <= 1 req/sec
            js = r.json() if r.status_code == 200 else []
        except Exception:
            js = []
        if js:
            val = (float(js[0]["lon"]), float(js[0]["lat"]),
                   js[0].get("display_name", ""))
            break
    cache[key] = val
    _save(cache)
    return val


if __name__ == "__main__":
    import sys
    tests = sys.argv[1:] or [
        "Hoan Kiem Lake", "Temple of Literature", "Ho Chi Minh Mausoleum",
        "Long Bien Bridge", "St Joseph's Cathedral", "Dong Xuan Market",
        "West Lake", "Hanoi Opera House", "Tran Quoc Pagoda",
        "Imperial Citadel of Thang Long", "Hoa Lo Prison", "Bat Trang",
    ]
    c = _load()
    for t in tests:
        v = geocode(t, c)
        print(f"  {t:36s} -> " + (f"{v[0]:.5f},{v[1]:.5f}  {v[2][:60]}" if v else "NOT FOUND"))
