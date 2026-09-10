"""Reproduce every style measurement quoted in STYLE.md and README.md.

Usage:
    PYTHONPATH=. .venv/bin/python tools/measure_style.py [path/to/london_o.jpg]

The 6137x6137 London original is Fischer's copyrighted image and is NOT
redistributed in this repo; pass its path to run the [V] measurements. Without
it, only the measurements on our own output run.
"""
import math
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

OURS = "out/hanoi_locals_tourists_6137.png"
BASEMAP = (170, 170, 0)


def _load(path):
    return np.asarray(Image.open(path).convert("RGB")).astype(np.int16)


# ---------------------------------------------------------------- basemap hue
def basemap_hue(a, label, ts=32, max_ink_frac=0.01):
    """Integrated channel deficit over near-data-free tiles -> R:G:B removal.

    #AAAA00 removes a third of R and G and all of B, so the ratio should be
    0.333 : 0.333 : 1.000.

    Tiles must contain basemap ink and almost no data ink. Requiring ZERO data
    ink over 128px tiles finds nothing at all on the London original (it is
    dense enough that every large tile catches a dot), which an earlier version
    of this function did while returning silently. It now reports when it has
    nothing to measure.

    On the London original this gives 0.353 : 0.369 : 1.000 over 70 tiles -
    within ~6-11% of #AAAA00's 0.333, decisively excluding #FFFF00 (0.000) and
    #BBBB00 (0.267), but NOT cleanly separating #AAAA00 from #999900 (0.400).
    A tighter hand-built tile set does separate them: 0.344 : 0.351 over 20
    tiles. The residual bias here is JPEG-blurred data ink leaking into tiles;
    tightening `max_ink_frac` finds too few tiles to average. Treat this as
    confirming the hue, not as a 3-decimal value.
    """
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    data = (B - R > 30) | (R - B > 30)
    base = (np.abs(R.astype(int) - G) < 25) & (R.astype(int) - B > 40)
    S = a.shape[0]
    dr = dg = db = 0
    used = 0
    for y in range(0, S - ts, ts):
        for x in range(0, S - ts, ts):
            d = data[y:y + ts, x:x + ts]
            if d.mean() > max_ink_frac:
                continue
            if not base[y:y + ts, x:x + ts].any():
                continue
            t = a[y:y + ts, x:x + ts].astype(np.int64)
            dr += (255 - t[:, :, 0]).sum()
            dg += (255 - t[:, :, 1]).sum()
            db += (255 - t[:, :, 2]).sum()
            used += 1
    if used == 0 or db == 0:
        print(f"  {label}: NO usable near-data-free tiles found - nothing "
              f"measured (loosen ts / max_ink_frac)")
        return
    print(f"  {label}: {used} near-data-free {ts}px tiles, deficit ratio "
          f"{dr/db:.3f} : {dg/db:.3f} : 1.000   (#AAAA00 predicts 0.333)")
    print(f"     #999900 predicts 0.400, #BBBB00 0.267, #FFFF00 0.000. "
          f"#FFFF00 is decisively excluded; this tile set does not cleanly "
          f"separate #AAAA00 from #999900 (the audit's tighter tile set does)")


# ------------------------------------------------------------- line opacity
def isolated_line_alpha(a, label):
    """Alpha of ISOLATED single blue/red segments, read straight off a channel.

    For blue ink at alpha `x` over white, R = 255(1-x) exactly. So no estimator
    and no calibration is involved. The mask below is the whole method:
      - blue-ish, and on a long straight thin structure (15px directional open)
      - >= 3 px from any 3x3 square core
      - no basemap and no opposite-colour ink within 2 px
      - locally sparse: < 3% blue coverage in a 31x31 window (so NOT a bundle)
    """
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    out = {}
    for name, ink, chan in (("blue", (B - R > 40) & (B - G > 40), R),
                            ("red", (R - B > 40) & (R - G > 40), B)):
        cores = ndimage.binary_erosion(ink, np.ones((3, 3)))
        near_pt = ndimage.binary_dilation(cores, np.ones((7, 7)))
        base = np.all(a == np.array(BASEMAP), axis=2)
        other = (R - B > 40) & (R - G > 40) if name == "blue" else \
                (B - R > 40) & (B - G > 40)
        near_bad = ndimage.binary_dilation(base | other, np.ones((5, 5)))
        dens = ndimage.uniform_filter(ink.astype(np.float32), 31)
        cand = ink & ~near_pt & ~near_bad & (dens < 0.03)
        keep = np.zeros_like(cand)
        for ang in range(0, 180, 15):
            L = 15
            se = np.zeros((L, L), bool)
            c = L // 2
            th = np.deg2rad(ang)
            for t in np.linspace(-c, c, 4 * L):
                yy = int(round(c + t * math.sin(th)))
                xx = int(round(c + t * math.cos(th)))
                if 0 <= yy < L and 0 <= xx < L:
                    se[yy, xx] = True
            keep |= ndimage.binary_opening(cand, se)
        v = chan[keep]
        if v.size:
            med = float(np.median(v))
            print(f"  {label} {name}: n={v.size} median channel={med:.0f} "
                  f"-> alpha {1 - med/255:.3f}")
            out[name] = 1 - med / 255
    return out


def accumulation_rungs(a, label):
    """Distinct ink levels, which should be 255*(1-alpha)^n for n = 1, 2, 3...

    Only meaningful on a LOSSLESS file. On the JPEG original the exact-value
    masks below are destroyed by compression; use density stratification there
    (see london_line_strata).
    """
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    print(f"  {label}:")
    for name, ink, chan in (("blue over white", (B == 255) & (R == G) & (R < 250) & (R > 0), R),
                            ("red over white", (R == 255) & (G == B) & (G < 250) & (G > 0), G)):
        vals, counts = np.unique(chan[ink], return_counts=True)
        top = sorted(zip(vals, counts), key=lambda x: -x[1])[:6]
        print(f"     {name}: " + "  ".join(f"{v}({c})" for v, c in sorted(top)))
    print("     predicted for alpha 0.55: " +
          "  ".join(str(int(round(255 * 0.45 ** n))) for n in (1, 2, 3, 4, 5)))


def longest_straight_line(a, label, close=True):
    """Longest straight thin data-ink run, by directional opening.

    The gap closing is not optional on a JPEG: a 1-px line in q75 4:2:0 drops
    pixels, and an opening then breaks at every dropout, which UNDER-measures
    (0.79 km without closing vs 1.07-1.19 km with it on the same file).
    """
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    ink = ((B - R > 40) & (B - G > 40)) | ((R - B > 40) & (R - G > 40))
    cores = ndimage.binary_erosion(ink, np.ones((3, 3)))
    thin = ink & ~ndimage.binary_dilation(cores, np.ones((5, 5)))
    if close:
        thin = ndimage.binary_closing(thin, np.ones((3, 3)))
    best = 0
    for L in (50, 100, 150, 200, 271, 301, 351):
        found = False
        for ang in range(0, 180, 10):
            se = np.zeros((L, L), bool)
            c = L // 2
            th = np.deg2rad(ang)
            for t in np.linspace(-c, c, 4 * L):
                yy = int(round(c + t * math.sin(th)))
                xx = int(round(c + t * math.cos(th)))
                if 0 <= yy < L and 0 <= xx < L:
                    se[yy, xx] = True
            if ndimage.binary_opening(thin, se).any():
                found = True
                break
        if found:
            best = L
        else:
            break
    mpp = 0.217705 * 111320.0 / a.shape[0]
    print(f"  {label}: longest straight thin run survives a {best} px opening "
          f"= {best*mpp/1000:.2f} km  (openings tried up to 350 px)")


def ink_split(path):
    a = _load(path)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    pt = (np.all(a == np.array([0, 0, 255]), axis=2)
          | np.all(a == np.array([255, 0, 0]), axis=2)
          | np.all(a == np.array([255, 255, 0]), axis=2))
    ink = ((B - R > 20) & (B - G > 20)) | ((R - B > 20) & (R - G > 20)) | \
          ((R > 200) & (G > 200) & (B < 200) & ~np.all(a == np.array(BASEMAP), axis=2))
    ln = ink & ~pt
    npt, nln = int(pt.sum()), int(ln.sum())
    print(f"  point ink {npt} px, line ink {nln} px -> lines are "
          f"{100*nln/(npt+nln):.1f}% of the data ink")
    # NOT measured here: pixels carrying more than one line class. A colour
    # test cannot see a blue/red crossing - "bluer than red" and "redder than
    # blue" are mutually exclusive, so such a mask only ever finds
    # yellow-over-red. The build computes it from the drawing ops instead and
    # records it in out/stats.json as cross_class_line_px.


def colour_model(path="data/ref_london.jpg"):
    """The area-average ink model, and why '99.2% invert cleanly' means little."""
    if not os.path.exists(path):
        return
    a = _load(path).astype(np.float64) / 255.0
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    ab = 1 - R
    ar = (1 - G) - ab
    ay = (1 - B) - ar
    for tol in (0.0, 8 / 255):
        ok = (ab >= -tol) & (ar >= -tol) & (ay >= -tol)
        print(f"  ink model on {os.path.basename(path)}: "
              f"{100*ok.mean():.2f}% of pixels invert with non-negative ink "
              f"at tolerance {tol*255:.0f}/255")
    print("  NOTE: all six permutations of the blue/red/yellow ink roles score "
          "identically, and a no-model baseline scores 100%, so this figure "
          "cannot discriminate the model. Do not cite it as support.")


if __name__ == "__main__":
    print("== our own output ==")
    if os.path.exists(OURS):
        ours = _load(OURS)
        isolated_line_alpha(ours, "ours (true alpha 0.55)")
        accumulation_rungs(ours, "ours")
        ink_split(OURS)
    print("\n== colour model on the published 800px downscale ==")
    colour_model()
    lon = sys.argv[1] if len(sys.argv) > 1 else None
    if lon and os.path.exists(lon):
        print("\n== London original (6137px) ==")
        L = _load(lon)
        basemap_hue(L, "london")
        isolated_line_alpha(L, "london")
        print("     CAVEAT: mask-sensitive. This implementation gives "
              "0.64 blue / 0.53 red; two independent implementations of the "
              "same written selection give 0.55/0.54 and 0.66/0.58. The robust "
              "conclusion is ~0.55 with accumulation, NOT a 3-decimal value.")
        longest_straight_line(L, "london (gap-closed)")
        print("     CAVEAT: this figure is highly mask-sensitive. This "
              "implementation reaches 0.59 km; the project's style audit, with "
              "a differently-built thin mask, reaches 1.07-1.19 km. STYLE.md "
              "quotes the audit's figure and flags the spread. Treat the "
              "conclusion (our 4.0 km chords are several times anything "
              "measurable in the original) as the finding, not the number.")
    else:
        print("\n(no London original supplied; [V] measurements skipped)")
