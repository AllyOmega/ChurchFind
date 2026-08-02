#!/usr/bin/env python3
"""Score Anglican parishes from their linked Wikipedia articles.

OpenStreetMap carries `wikipedia` and `wikidata` tags on many church features,
which makes this an *exact* join -- no fuzzy name matching, no guessing whether
"St Mary's, Springfield" is the right St Mary's. About 420 US Anglican churches
carry one, and roughly two thirds of those have no website recorded, so this
reaches parishes the site scrape cannot.

WHAT WIKIPEDIA IS GOOD AND BAD AT

An article is usually about a *building*: its architect, its NRHP listing, the
fire of 1908. Where it does describe worship it is often excellent -- "well
known as a prominent center of Anglo-Catholic worship" is exactly the sentence
that settles the question -- but the median article says nothing about it at
all. Measured over the 384 linked English articles, 27% produced any score.

It is also written by someone other than the parish, possibly years ago, and
describes history rather than next Sunday. So a Wikipedia reading is capped
lower than a website reading (`SOURCE_CAPS` in churchmanship.py) and the UI
names the source.

CACHING

Article text is cached on disk, not just the score. Fetching is the expensive,
externally-visible part; scoring is free and the lexicon changes. A lexicon fix
should be a rescore, not a re-crawl.

Usage:
    python scraper/fetch_wikipedia.py            # fetch and score everything
    python scraper/fetch_wikipedia.py --rescore  # reuse cached text, no network
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import churchmanship  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "wikipedia-cache.json"
OUTPUT = ROOT / "data" / "churchmanship-wikipedia.json"

USER_AGENT = "ChurchFindBot/1.0 (+https://github.com/AllyOmega/ChurchFind; church directory)"
TIMEOUT = 30
DELAY = 0.2               # Wikipedia's API is generous; this is still polite
WIKIDATA_BATCH = 40       # ids per wbgetentities call


def api(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def english_title(tag):
    """`wikipedia=en:Some Article` -> `Some Article`, else None.

    Non-English tags are skipped rather than translated: the lexicon is English
    and scoring a German article with it would produce confident nonsense.
    """
    if not tag:
        return None
    if ":" not in tag:
        return tag
    language, _, title = tag.partition(":")
    return title if language.strip().lower() == "en" else None


def resolve_wikidata(ids):
    """Wikidata Q-ids -> English Wikipedia titles, in batches."""
    titles = {}
    for start in range(0, len(ids), WIKIDATA_BATCH):
        batch = ids[start:start + WIKIDATA_BATCH]
        joined = urllib.parse.quote("|".join(batch))
        try:
            data = api("https://www.wikidata.org/w/api.php?action=wbgetentities"
                       f"&format=json&props=sitelinks&sitefilter=enwiki&ids={joined}")
        except Exception:                                   # noqa: BLE001
            continue
        for qid in batch:
            entity = data.get("entities", {}).get(qid, {})
            link = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
            if link:
                titles[qid] = link
        time.sleep(DELAY)
    return titles


def fetch_extract(title):
    """Plain-text article body, or "" if it cannot be had.

    `exlimit` is silently capped at 1 whenever full-text extracts are requested,
    so batching titles here returns one extract and quietly drops the rest.
    These go one at a time on purpose.
    """
    try:
        data = api("https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
                   "&explaintext=1&exlimit=1&redirects=1&format=json"
                   f"&titles={urllib.parse.quote(title)}")
    except Exception:                                       # noqa: BLE001
        return ""
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            return ""
        return page.get("extract", "") or ""
    return ""


def load_cache():
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rescore", action="store_true",
                        help="reuse cached article text, make no requests")
    parser.add_argument("--db", help="database to read church wiki tags from")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "server"))
    from db import connect

    connection = connect(args.db)
    rows = connection.execute(
        "SELECT id, name, wikipedia, wikidata FROM churches "
        "WHERE family = 'anglican' AND (wikipedia <> '' OR wikidata <> '') ORDER BY id"
    ).fetchall()
    print(f"{len(rows)} Anglican parishes carry a wiki tag.")

    cache = load_cache()

    # Titles first: prefer the explicit wikipedia tag, fall back to wikidata.
    titles = {}
    unresolved = []
    for row in rows:
        title = english_title(row["wikipedia"])
        if title:
            titles[row["id"]] = title
        elif row["wikidata"]:
            unresolved.append(row)

    if unresolved and not args.rescore:
        pending = [r["wikidata"] for r in unresolved if r["id"] not in cache]
        if pending:
            print(f"Resolving {len(pending)} wikidata ids...", flush=True)
            resolved = resolve_wikidata(pending)
            for row in unresolved:
                if row["wikidata"] in resolved:
                    titles[row["id"]] = resolved[row["wikidata"]]
    for row in unresolved:
        if row["id"] in cache and cache[row["id"]].get("title"):
            titles.setdefault(row["id"], cache[row["id"]]["title"])

    print(f"{len(titles)} resolve to an English article.\n", flush=True)

    scores, counts = {}, {}
    for position, row in enumerate(rows, start=1):
        church_id = row["id"]
        title = titles.get(church_id)
        if not title:
            counts["no-english-article"] = counts.get("no-english-article", 0) + 1
            continue

        entry = cache.get(church_id)
        if entry is None or (not args.rescore and not entry.get("text")):
            if args.rescore:
                counts["not-cached"] = counts.get("not-cached", 0) + 1
                continue
            text = fetch_extract(title)
            entry = {"title": title, "text": text}
            cache[church_id] = entry
            time.sleep(DELAY)

        text = entry.get("text", "")
        if not text:
            counts["no-article-text"] = counts.get("no-article-text", 0) + 1
            continue

        result = churchmanship.score(text, source="wikipedia")
        if result:
            scores[church_id] = result
            counts["scored"] = counts.get("scored", 0) + 1
            print(f"  [{position}/{len(rows)}] {row['name'][:40]:<42} "
                  f"cer {result['ceremonial']:+.2f} theo {result['theology']:+.2f} "
                  f"conf {result['confidence']:.2f}", flush=True)
        else:
            counts["no-signal"] = counts.get("no-signal", 0) + 1

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
    OUTPUT.write_text(json.dumps({"scores": scores}, indent=1, sort_keys=True))

    print("\n" + "  ".join(f"{name}: {count}" for name, count in sorted(counts.items())))
    print(f"Wrote {len(scores)} scores to {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
