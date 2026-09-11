# Crawling tooling and metadata sources

Two questions, answered by running things rather than reading docs:

1. what is the best free image/metadata crawling tooling available today, and
2. can any social platform supply lat/lon + stable photographer id + date,
   **plus** a queryable worldwide per-photographer history — the field the map's
   three colours are actually computed from.

Everything below was measured on this machine on 2026-09-10, against live
services, from `/home/baoro/stuff/random/img-city-heatmap/.venv`
(Python 3.14.6, 16 cores, 6 GB RAM). Numbers are what the run printed. Where a
claim is documentation rather than observation it is marked **[undemonstrated]**.

No existing project file was modified. Packages were added to the venv;
`playwright` stayed at 1.62.0 throughout.

---

# Deliverable 1 — the tooling

## 1.1 Bulk fetching images whose URLs we already have

The project's current fetcher is `lat/relocate_fetch.py`, and its inner loop is

    resp = requests.get(url_for(r, size), timeout=25)

inside `ThreadPoolExecutor(16)`. The important detail is that it calls the
module-level `requests.get`, which **builds a fresh `Session` — and therefore a
fresh TCP connection and TLS handshake — for every single image.** That is the
single biggest thing wrong with it, and it is a two-line fix.

Method: 1,800 real `live.staticflickr.com/…_z.jpg` URLs drawn from
`data/yfcc/hanoi_wide.tsv`, ~85 KB average. **Every tool got its own disjoint
1,800-URL sample**, because Flickr's CDN warms hard — re-running the identical
list took `plain w=16` from 35.8 to 79.9 img/s, a 2.2× illusion. Any benchmark
that reuses one URL list is measuring the CDN, not the tool.

| configuration | wall | img/s | MB/s | fetched |
|---|---|---|---|---|
| `requests.get`, 16 workers — **current project code** | 41.6 s | **36.3** | 3.54 | 1512/1800 |
| `requests.Session` pooled, 16 workers | (300-URL run) | 48.3 | 4.77 | 258/300 |
| `requests.Session` pooled, 64 workers | 8.2 s | **186.3** | 18.16 | 1521/1800 |
| `requests.Session` pooled, 64 workers (repeat, fresh sample) | 8.1 s | **188.8** | 18.23 | 1527/1800 |
| `httpx` async, 64 concurrent | 13.7 s | 111.0 | 11.02 | 1526/1800 |
| `img2dataset` 8 proc × 32 thr, **default sharding** | 43.4 s | 49 | — | 83.5% |
| `img2dataset` 8 proc × 32 thr, 8 shards | 19.5 s | 134 | — | 83.2% |
| `img2dataset` 16 proc × 64 thr, 16 shards | 20.5 s | **142** | — | 84.1% |
| `gallery-dl` (serial; fetched 2.3 MB originals) | 57.4 s / 113 img | 2.0 | 4.5 | 113/120 |

The ~15% miss rate is dead Flickr photos (deleted or made private since 2014),
consistent across every tool. The project's real fetcher retries sizes `z, c, ""`
and so recovers some of these; my harness did not, deliberately, to keep the
comparison clean.

### Does img2dataset beat what we have? No — and the answer is not close.

**img2dataset's best measured run, 142 img/s, is 0.76× a tuned
`ThreadPoolExecutor`'s 188 img/s, and it burned 519% CPU to get there against
the thread pool's near-nil.** At 1,800 URLs the winner is 21 lines of
`requests.Session`.

That is not a criticism of img2dataset, it is a scale mismatch. img2dataset is
built for LAION-scale work — 100M–5B URLs, distributed over Spark or pyspark
workers, writing `webdataset`/parquet shards, resizing on the way through. All
of that machinery is overhead at 1,800 URLs, or even at this project's full
23,775. The crossover is somewhere in the millions, and this project is three
orders of magnitude below it.

**Recommendation (a): do not adopt img2dataset. Add a pooled `Session` to
`relocate_fetch.py` and raise the worker count.** Measured 36.3 → 188.8 img/s,
a **5.2× speedup**, for this:

```python
_tl = threading.local()
def _sess(workers):
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session()
        s.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=workers, pool_maxsize=workers))
        _tl.s = s
    return s
```

then `_sess(workers).get(...)` in place of `requests.get(...)`, with
`workers=64`. Note 64 workers is not itself the win — `plain w=64` only reached
78.0 img/s, still handshaking every time. **The connection reuse is the win;
the worker count only pays off once handshakes stop dominating.** 18 MB/s is
also close to this link's ceiling, so there is little left above 64.

### img2dataset's three landmines, if you ever do go to that scale

1. **It does not run on Python 3.14 at all.** `ModuleNotFoundError: No module
   named 'imghdr'` — `imghdr` was deleted from the stdlib in 3.13.
   `pip install standard-imghdr` fixes it (and warns you it is a shim).
2. **It silently drops rows whose metadata isn't JSON-scalar.** A `date` column
   became a pandas `Timestamp`, and 10% of items died with
   `Object of type Timestamp is not JSON serializable` — reported as
   *"failed to download"*, which is a lie; the bytes arrived fine. Pass dates as
   epoch ints.
3. **Its default sharding silently defeats `--processes_count`.**
   `--number_sample_per_shard` defaults to 10,000, so 1,800 URLs became
   *one* shard and one process no matter what you asked for: 49 img/s against
   142 once sharded to 8/16. The log line that tells you is
   `File sharded in 1 shards`.

Credit where due: img2dataset **does** carry per-item metadata properly, which
was one of the questions. `--save_additional_columns` round-trips arbitrary
columns into both a per-image `.json` sidecar and a parquet manifest, *and* it
extracts EXIF for free:

```json
{"photo_id": 5961379594, "user": "77987497@N00", "taken_epoch": 1310271719,
 "lat": 21.297294, "lon": 106.243047, "status": "success",
 "exif": "{\"Image ExifOffset\": \"38\", \"Image GPSInfo\": \"44\"}", "sha256": "125b6a08…"}
```

The parquet manifest also records `failed_to_download` rows with their metadata
intact, which is genuinely better bookkeeping than the current code's
`return None`. If you want one idea from img2dataset without the dependency,
it is that: **keep a manifest row per attempted URL, with its outcome.**

## 1.2 Crawling pages to discover new geotagged items

Same task for every candidate: start at `commons.wikimedia.org/wiki/Category:Hanoi`,
reach `File:` pages, and pull out coordinates + uploader + date. This is a fair
stand-in for the real job because the metadata is in the HTML, so a tool either
carries per-item fields or it doesn't.

| tool | install | robots.txt by default | measured | carries per-item metadata? |
|---|---|---|---|---|
| **httpx + selectolax** (hand-rolled, ~50 lines) | trivial, 2 wheels | you write it | **120 pages / 2.4 s = 50.6 pages/s**, 86 geotagged (72%) | yes — whatever you select |
| **crawlee-python** 1.10.0 | one wheel | **no** (`respect_robots_txt_file=False`) | 36 pages / 6.7 s = 5.4 pages/s (334 req/min) | yes, via `Dataset` |
| **scrapy** 2.19.0 | one wheel | **library default False**, template True | 68 items / 23.7 s = 2.9 items/s (throttled: `DOWNLOAD_DELAY=0.25`, conc 8) | yes — Items + Feed exports |
| **scrapy-playwright** 0.0.48 | one wheel, reused existing chromium | inherits scrapy | 50 items from 5 JS-rendered pages / 2.6 s incl. browser launch ≈ 0.5 s/page | yes |
| **photon** v1.3.2 (git clone + `tld`) | 2 min | none | 78 req / 3 s = 25 req/s | **no — URL lists only** |
| **katana** v1.7.0 (67 MB Go binary) | 1 min | **none, no such flag exists** | 8,585 URLs, **0 usable** | **no** |
| **you-get** 0.4.1743 | one wheel | none | returned page banner logos | **no** |
| **bbot** 3.0.2 | 42 deps, 205 MB venv | n/a | not run — see below | **no** |
| **gallery-dl** 1.32.11 | one wheel | none | **300 items / 9.8 s = 30.6 items/s, 300/300 with coordinates** | **yes, richly** |

### The surprise: hand-rolled won by 9×

`httpx` + `selectolax` did the same job **9.4× faster than crawlee and 17×
faster than scrapy**, in about fifty lines, with full metadata. selectolax is a
Lexbor binding, so parsing is roughly an order of magnitude cheaper than
`lxml`/`BeautifulSoup`, and at this scale the frameworks' schedulers, dupe
filters, middleware chains and disk-backed request queues are pure cost.

Some of that gap is my configuration (I gave scrapy a 0.25 s delay and
concurrency 8, deliberately polite), so treat 17× as "scrapy as I chose to run
it", not scrapy's ceiling. The crawlee comparison is fairer — it auto-scaled
itself and still came in at 5.4 pages/s.

**What the frameworks buy you** is the stuff you only miss on run 200: retry
policies, request de-duplication, session/proxy rotation, resumable queues,
per-domain throttling, structured feed exports. For a one-off harvest of a few
hundred thousand pages against one cooperative host, that is not worth 9× — and
this project's harvest is exactly that shape.

**Recommendation (b), split by target:**

- **A cooperative host with an API or clean HTML (Commons, Flickr, iNaturalist):**
  hand-rolled `httpx` + `selectolax`, or better, skip HTML and use the API.
  50 pages/s measured, no framework.
- **JS-only pages** (map viewers that render markers client-side):
  **Playwright**, already installed and working here. ~0.5 s/page, ~2 pages/s at
  4 concurrent contexts — 25× slower than fetching HTML, so use it only where
  the coordinates genuinely aren't in the markup. Reach for `scrapy-playwright`
  over raw Playwright only if you also want scrapy's queue.
- **A crawl you'll run repeatedly across many sites**, where resumability and
  politeness matter more than speed: **scrapy**. It is the only tool here whose
  project template turns robots.txt obedience *on*.
- **Anything gallery-dl already has an extractor for: use gallery-dl.** See below.

### gallery-dl is the standout, and not for the reason I expected

**3,821 extractors**, and the Flickr one turns out to be a complete solution to
this project's data problem rather than merely an image downloader:

```
$ gallery-dl -j -o metadata="geo,date_taken,geo_is_public" \
    "https://www.flickr.com/photos/29858421@N04/"
{'id': 55479073529, 'latitude': '50.248714', 'longitude': '11.206739',
 'datetaken': '2026-07-04 13:33:16', 'accuracy': '16', 'geo_is_public': 1}
owner: {'nsid': '29858421@N04', 'path_alias': 'danielmennerich', 'username': 'Daniel Mennerich'}
```

That is all three required fields plus a stable id, from a **per-user** endpoint
— i.e. the worldwide history. `-j` dumps metadata without downloading pixels, at
**30.6 items/s**, all 300 with coordinates. There are also
`FlickrSearchExtractor` (discovery) and 3,819 others including Bluesky,
Instagram, Mastodon, SmugMug and Wikimedia.

Two caveats. **gallery-dl has no concurrency whatsoever** — zero matches for
thread/concurrent/parallel/worker in `--help` — so it downloaded images at
2.0 img/s serial, 94× slower than the tuned pool. Use it for *metadata and
coverage*, and hand its URLs to the thread pool for pixels. And it **never
reads robots.txt**; it is a "fetch what the user asked for" tool, and the
politeness is your responsibility.

### The four tools that are simply the wrong genre

**katana** and **photon** and **bbot** are security-recon tools. They enumerate
attack surface — URLs, endpoints, parameters, subdomains, secrets. They have no
concept of a per-item record, so they cannot carry a geotag even in principle.

katana failed concretely as well as conceptually: of 8,585 URLs it emitted,
**zero** were `File:` pages, because **it truncates URLs at the colon** —
2,691 hits for `/wiki/Special` (not `Special:Something`), 202 for `/wiki/Category`.
Every MediaWiki title got mangled. It also leaked scope into every
`*.wikipedia.org` language domain, has a **default rate limit of 150 req/s**,
and `-h` contains **zero** occurrences of "respect" or "obey" — there is no
robots.txt option at all. Its `-kf robotstxt` flag *reads* robots.txt as a
source of URLs to visit, which is close to the opposite.

photon ran fine (25 req/s) and produced `files.txt` / `internal.txt` /
`external.txt` / `fuzzable.txt` — URL lists, with **no coordinates anywhere**
(grep for a decimal-degree pattern across its whole output: no matches).

**bbot** installs 42 packages and a 205 MB venv (ansible-core, ansible-runner,
yara-python). Its 439-module inventory contains no image, photo or EXIF module;
its only "geo" modules are IP2Location and IPStack, i.e. **IP** geolocation,
which is not a photo geotag and would be exactly the kind of inference this
project has ruled out. **I did not run it against a third-party host**: bbot's
default presets do active attack-surface scanning, and pointing that at
Wikimedia to satisfy a benchmark is not defensible. Installed, inventoried,
declined.

**you-get** is a video-site downloader. Pointed at a Commons `File:` page it
returned the page's *banner logos* (`Wiki_Loves_Folklore_Logo.svg`) with
`"url": null`, and its metadata model is `title`/`container`/`size`. Not
applicable.

## 1.3 robots.txt: what these tools actually do, measured

Do not trust reputation here. I read the code.

| tool | default | evidence |
|---|---|---|
| scrapy | **False** as a library; **True** via `startproject` | `scrapy/settings/default_settings.py:549` = `ROBOTSTXT_OBEY = False`; `scrapy/templates/project/module/settings.py.tmpl:22` = `True` |
| crawlee | **False** | `crawlee/crawlers/_basic/_basic_crawler.py:304` — `respect_robots_txt_file: bool = False` |
| img2dataset | never checks | no `robots` reference anywhere in the package |
| gallery-dl | never checks | same |
| you-get | never checks | same |
| katana | no such option | 0 hits for respect/obey in `-h` |
| photon | never checks | — |

**`scrapy runspider` gets the library default, not the template default.** My
spider only obeyed robots.txt because I set `ROBOTSTXT_OBEY: True` by hand. A
standalone spider file is silently in the permissive mode.

### Two robots.txt traps worth writing down

**`urllib.robotparser` fails closed and silently, for the wrong reason.** My
hand-rolled harvester reported `/wiki/Category:Hanoi allowed=False`, which is
wrong — scrapy's protego had just crawled it happily. The cause:
`RobotFileParser.read()` fetches robots.txt *itself*, with Python's default
`Python-urllib/3.14` User-Agent, and **Wikimedia returns 403 to that UA**;
`read()` swallows the error and sets `disallow_all = True`. Handed the same
bytes, both parsers agree:

| path | urllib | protego |
|---|---|---|
| `/wiki/Category:Hanoi` | True | True |
| `/wiki/File:Drying_incense_08.jpg` | True | True |
| `/w/api.php?action=query` | False | False |
| `/wiki/Special:Search` | False | False |

So: fetch robots.txt yourself with your real UA, then `parse()` the text. Never
let `read()` do it.

**Commons robots.txt disallows `/w/` — which is the MediaWiki API.** For
`User-agent: *` the file says `Disallow: /w/` and `Disallow: /api/`, with narrow
`Allow:` exceptions for `action=mobileview` and `load.php`. Read literally, a
robots-obeying crawler may not call `api.php` at all. That is aimed at search
crawlers, and Wikimedia separately publishes an API etiquette policy that
positively invites API use with a descriptive UA and serial requests — but if
you point a `ROBOTSTXT_OBEY=True` scrapy at `api.php` it will refuse, and that
refusal is correct-per-the-file. Use the API under its own terms, with a real
User-Agent, and don't route it through a robots-obeying crawler.

**A Scrapy 2.19 trap, unrelated to robots.** My first scrapy-playwright spider
scraped 0 items in 0.4 s and **exited 0**. Cause: Scrapy 2.19 replaced
`start_requests()` with `async def start()`, and an old-style `start_requests`
is now simply never called — no error, no warning, no requests. Every
scrapy-playwright example on the internet still uses `start_requests`.

## 1.4 Deliverable 1 recommendations, condensed

| need | use | why |
|---|---|---|
| **(a) bulk-fetch known URLs** | `requests.Session` (pooled) + `ThreadPoolExecutor(64)` | 188.8 img/s measured vs 36.3 today = **5.2×**. img2dataset's best was 142. |
| **(b) discover new geotagged items — API/clean HTML** | API first; else `httpx` + `selectolax` | 50.6 pages/s, ~50 lines |
| **(b) discover — JS-rendered maps** | Playwright (installed) | ~0.5 s/page; only where coords aren't in HTML |
| **(b) discover — any site gallery-dl covers** | `gallery-dl -j -o metadata=…` | 3,821 extractors, 30.6 items/s, all three fields |
| repeatable multi-site crawls | scrapy | only tool that defaults to obeying robots (via template) |
| never | katana, photon, bbot, you-get | wrong genre; no per-item metadata; katana mangles URLs |

Full `pip` delta added to the venv: `httpx selectolax gallery-dl you-get
img2dataset scrapy scrapy-playwright crawlee[beautifulsoup] standard-imghdr tld`.
`playwright` untouched at 1.62.0. `standard-imghdr` is only needed by
img2dataset and can be dropped with it. bbot lives in a throwaway venv under the
scratchpad, not in the project.

