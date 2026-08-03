#!/usr/bin/env python3
"""Fetch Anglican parish websites and score their churchmanship.

Unlike the Overpass scrape, this reads other people's servers, so it behaves
like a crawler is supposed to:

* **robots.txt is honoured**, fetched once per host and cached. A disallowed
  path is skipped, not fetched-and-discarded.
* **One request at a time, with a delay between hosts**, and `Crawl-delay` is
  respected where a site sets one.
* **A real User-Agent** naming the project and a contact URL, so anyone reading
  their logs can see who this is and tell us to stop.
* **Two pages per parish at most** -- the homepage and, if linked, one page that
  looks like it describes worship. This is not a general crawl.
* **Results are cached on disk**, so a rerun does not re-fetch what it already
  has. Deleting `data/sites-cache/` forces a refresh.

Usage:
    python scraper/fetch_sites.py --limit 100      # a sample
    python scraper/fetch_sites.py                  # every Anglican site
    python scraper/fetch_sites.py --refresh        # ignore the cache
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import churchmanship  # noqa: E402
import parse_prose_times  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "sites-cache"
OUTPUT = ROOT / "data" / "churchmanship.json"
# Service times read off the same pages. Only 204 of 2,812 Anglican churches
# have one in OpenStreetMap; the parish's own website is where they actually are.
TIMES_OUTPUT = ROOT / "data" / "service-times-web.json"

USER_AGENT = "ChurchFindBot/1.0 (+https://github.com/AllyOmega/ChurchFind; church directory)"
TIMEOUT = 20
DEFAULT_DELAY = 2.0          # seconds between requests to the same host
POLITE_GAP = 0.5             # seconds between requests generally
MAX_BYTES = 600_000

# Anchor text that suggests a page describing worship, in rough priority order.
WORSHIP_HINTS = [
    "worship", "services", "service times", "sunday", "liturgy", "mass",
    "about us", "who we are", "what to expect",
]


class TextExtractor(HTMLParser):
    """Strip a page to visible text. Not a browser -- good enough for keywords."""

    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self._skip_depth = 0
        self._href = None
        self._anchor = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._anchor).strip().lower()))
            self._href = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)
            if self._href is not None:
                self._anchor.append(text)

    def text(self):
        return re.sub(r"\s+", " ", " ".join(self.parts))


_robots_cache = {}


def robots_allows(url):
    """True when robots.txt permits us, plus any Crawl-delay it asks for.

    A robots.txt we cannot fetch is treated as permissive, which is the
    conventional reading -- a 404 is not a prohibition.
    """
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in _robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(urllib.parse.urljoin(origin, "/robots.txt"))
        try:
            request = urllib.request.Request(
                urllib.parse.urljoin(origin, "/robots.txt"),
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                parser.parse(response.read().decode("utf-8", "replace").splitlines())
        except Exception:                                   # noqa: BLE001
            parser = None                                   # unreachable -> allowed
        _robots_cache[origin] = parser

    parser = _robots_cache[origin]
    if parser is None:
        return True, DEFAULT_DELAY
    delay = parser.crawl_delay(USER_AGENT) or DEFAULT_DELAY
    return parser.can_fetch(USER_AGENT, url), max(float(delay), DEFAULT_DELAY)


def fetch(url):
    """Fetch one page. Returns (text, links) or (None, []) on any failure."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return None, []
            raw = response.read(MAX_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, "replace")
    except Exception:                                       # noqa: BLE001
        return None, []

    parser = TextExtractor()
    try:
        parser.feed(body)
    except Exception:                                       # noqa: BLE001
        return None, []
    return parser.text(), parser.links


def pick_worship_link(base_url, links):
    """Choose at most one extra page that looks like it describes worship."""
    base = urllib.parse.urlparse(base_url)
    for hint in WORSHIP_HINTS:
        for href, anchor in links:
            if hint not in anchor:
                continue
            target = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlparse(target)
            # Same host only, and no fragment-only or mailto links.
            if parsed.netloc != base.netloc or parsed.scheme not in ("http", "https"):
                continue
            if target.rstrip("/") == base_url.rstrip("/"):
                continue
            return target
    return None


def cache_path(church_id):
    digest = hashlib.sha256(church_id.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{digest}.json"


def gather(church_id, url, refresh=False):
    """Fetch and score one parish. Returns a record, using the cache if allowed.

    A cached entry that kept the page text is rescored rather than reused.
    Fetching is the expensive, externally visible part; scoring is free and the
    lexicon changes, so a lexicon fix should never mean re-crawling other
    people's servers. Entries written before the text was cached still carry
    their old score and are left alone until a --refresh.
    """
    path = cache_path(church_id)
    if path.exists() and not refresh:
        try:
            record = json.loads(path.read_text())
            if record.get("text"):
                result = churchmanship.score(record["text"], source="website")
                record["score"] = result
                record["status"] = "scored" if result else "no-signal"
                record["times"] = parse_prose_times.to_pairs(record["text"])
                record["times_text"] = parse_prose_times.describe(record["text"])
            return record
        except json.JSONDecodeError:
            pass

    allowed, delay = robots_allows(url)
    if not allowed:
        record = {"id": church_id, "url": url, "status": "disallowed-by-robots"}
    else:
        text, links = fetch(url)
        time.sleep(delay)
        if text is None:
            record = {"id": church_id, "url": url, "status": "unreachable"}
        else:
            extra = pick_worship_link(url, links)
            if extra:
                extra_allowed, extra_delay = robots_allows(extra)
                if extra_allowed:
                    more, _ = fetch(extra)
                    time.sleep(extra_delay)
                    if more:
                        text = text + " " + more
            result = churchmanship.score(text, source="website")
            record = {"id": church_id, "url": url,
                      "status": "scored" if result else "no-signal",
                      # Kept so a lexicon change is a rescore, not a re-crawl.
                      "text": text[:200_000],
                      "score": result,
                      "times": parse_prose_times.to_pairs(text),
                      "times_text": parse_prose_times.describe(text)}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="only this many parishes")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache")
    parser.add_argument("--db", help="database to read church URLs from")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "server"))
    from db import connect

    connection = connect(args.db)
    rows = connection.execute(
        "SELECT id, name, website FROM churches "
        "WHERE family = 'anglican' AND website <> '' ORDER BY id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    print(f"{len(rows)} Anglican parishes with a website.\n")
    scores, times, counts = {}, {}, {}
    for position, row in enumerate(rows, start=1):
        record = gather(row["id"], row["website"], refresh=args.refresh)
        counts[record["status"]] = counts.get(record["status"], 0) + 1
        if record.get("score"):
            scores[row["id"]] = record["score"]
            score = record["score"]
            print(f"  [{position}/{len(rows)}] {row['name'][:42]:<44} "
                  f"cer {score['ceremonial']:+.2f} theo {score['theology']:+.2f} "
                  f"conf {score['confidence']:.2f}", flush=True)
        elif position % 25 == 0:
            print(f"  [{position}/{len(rows)}] ...", flush=True)
        if record.get("times"):
            times[row["id"]] = {"pairs": record["times"], "text": record["times_text"]}
        time.sleep(POLITE_GAP)

    OUTPUT.write_text(json.dumps({"scores": scores}, indent=1, sort_keys=True))
    TIMES_OUTPUT.write_text(json.dumps({"times": times}, indent=1, sort_keys=True))
    print("\n" + "  ".join(f"{name}: {count}" for name, count in sorted(counts.items())))
    print(f"Wrote {len(scores)} scores to {OUTPUT}")
    print(f"Wrote {len(times)} service-time sets to {TIMES_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
