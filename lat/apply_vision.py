"""Turn the vision pass's place names into coordinate overrides.

Place names are resolved through Nominatim, never asserted from memory. A
relocation is accepted only if the identification is confident enough AND the
geocoder puts it inside Fischer's Hanoi box - an identification that resolves
outside the city is a failed identification, not a long-distance move.

No jitter is added. An identification gives landmark- or street-level
precision, so drawing every photo of the Temple of Literature at the Temple of
Literature is the truthful representation of what we actually know; spreading
them out would invent precision we do not have.
"""
import glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lat.geocode import geocode, _load
from lat.fischer import HANOI_BOUNDS
from lat.relocate_fetch import load as load_pile_rows, pick_piles

MIN_CONFIDENCE = 0.6
VISION = "data/reloc/vision/*.json"
OUT = "data/reloc/overrides.json"


def main(min_conf=MIN_CONFIDENCE):
    s, w, n, e = HANOI_BOUNDS
    cache = _load()
    overrides, rejected, stats = {}, [], {"files": 0, "photos": 0, "named": 0,
                                          "low_conf": 0, "no_geocode": 0,
                                          "outside_box": 0, "accepted": 0}
    # A photo the vision pass places outside Hanoi was never taken in Hanoi:
    # one pile turned out to hold a floating fish farm in the Ha Long / Cat Ba
    # karst, ~150 km away. Drawing it at its pin would be a plain error, so
    # these are dropped from the map rather than relocated.
    exclude = []
    verdicts = {}
    for path in sorted(glob.glob(VISION)):
        try:
            d = json.load(open(path))
        except Exception as ex:
            print(f"  !! unreadable {path}: {ex}")
            continue
        stats["files"] += 1
        verdicts[d.get("pile")] = d.get("verdict")
        for p in d.get("photos", []):
            stats["photos"] += 1
            place = (p.get("place") or "").strip()
            if not place or place.upper() == "UNIDENTIFIED":
                continue
            stats["named"] += 1
            if float(p.get("confidence") or 0) < min_conf:
                stats["low_conf"] += 1
                continue
            g = geocode(place, cache)
            if not g:
                stats["no_geocode"] += 1
                rejected.append((p["id"], place, "no geocode"))
                continue
            lon, lat, disp = g
            if not (w <= lon <= e and s <= lat <= n):
                # Two different situations look alike here, and conflating them
                # deletes good data: the vision pass saying "this photo is not
                # in Hanoi", versus the geocoder resolving the wrong instance of
                # a common street name. "Nguyen Thai Hoc, Hanoi" is a central
                # Hanoi street, but Nominatim returned a namesake 20 km west -
                # dropping that photo would be acting on a geocoder error.
                # Only treat it as elsewhere when the identification itself does
                # not claim Hanoi.
                low = place.lower()
                claims_hanoi = ("hanoi" in low or "ha noi" in low
                                or "hà nội" in low)
                if claims_hanoi:
                    stats["no_geocode"] += 1
                    rejected.append((p["id"], place,
                                     f"geocoded outside Hanoi ({lon:.4f},{lat:.4f})"
                                     " but claims Hanoi -> left in place"))
                else:
                    stats["outside_box"] += 1
                    exclude.append(str(p["id"]))
                    rejected.append((p["id"], place,
                                     f"outside Hanoi ({lon:.4f},{lat:.4f}) -> dropped"))
                continue
            overrides[str(p["id"])] = [lon, lat]
            stats["accepted"] += 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(overrides, open(OUT, "w"), indent=1)
    json.dump(exclude, open("data/reloc/exclude.json", "w"), indent=1)
    # Pins the vision pass judged dumping grounds. A photo still sitting on one
    # of these, which the pass could not identify, has a coordinate we know to
    # be wrong: drawing it asserts a spot where no photograph was taken. Those
    # get masked from the map (but kept for classification - the city-level
    # geotag is still evidence the photographer was in Hanoi).
    false_pins = sorted(k for k, v in verdicts.items() if v == "dumping_ground")
    json.dump(false_pins, open("data/reloc/false_pins.json", "w"), indent=1)

    # Resolve those verdicts to explicit photo ids, using the SAME grouping the
    # contact sheets were built from. Keying the mask on a coordinate format
    # instead is fragile: when the renderer's pin identity changed, 8 of these
    # 10 keys silently stopped matching anything and 333 photos the vision pass
    # had adjudicated as wrongly placed went back onto the map while the README
    # still claimed they were masked. Ids do not drift.
    fp_set = set(false_pins)
    groups = {}
    for r in pick_piles(load_pile_rows()):
        groups.setdefault(r["pile"], []).append(r["id"])
    masked_ids, unresolved = [], []
    for key in false_pins:
        ids = [i for i in groups.get(key, []) if i not in overrides]
        if not groups.get(key):
            unresolved.append(key)
        masked_ids += ids
    # Validate BEFORE writing: a failing run must not leave a bad file that the
    # next build trusts. Same write-then-validate hazard as the awk cache.
    if unresolved:
        raise SystemExit("discredited pile keys resolved to no photos: "
                         + ", ".join(unresolved))
    json.dump(sorted(set(masked_ids)), open("data/reloc/masked_ids.json", "w"),
              indent=1)
    print("pile verdicts:")
    for k, v in sorted(verdicts.items()):
        print(f"   {k:22s} {v}")
    print("\nrelocation accounting:")
    for k in ("files", "photos", "named", "low_conf", "no_geocode",
              "outside_box", "accepted"):
        print(f"   {k:12s} {stats[k]}")
    if rejected:
        print("\nrejected identifications:")
        for i, pl, why in rejected[:15]:
            print(f"   {i:14s} {pl[:44]:44s} {why}")
    print(f"\nwrote {OUT} with {len(overrides)} overrides")
    print(f"wrote data/reloc/exclude.json with {len(exclude)} photos placed outside Hanoi")
    print(f"wrote data/reloc/false_pins.json with {len(false_pins)} discredited pile keys")
    print(f"wrote data/reloc/masked_ids.json with {len(set(masked_ids))} photo ids "
          f"on those piles (all {len(false_pins)} keys resolved)")
    return overrides


if __name__ == "__main__":
    main()
