# ChurchFind

A static site for finding a church near you anywhere in the United States, backed by a
scraped snapshot of OpenStreetMap.

Type a town or ZIP code, pick a denomination, drag the radius slider, and get a ranked
list with addresses, phone numbers, websites, service times and a map. No build step, no
server, no API keys — the whole thing is HTML, CSS, one script, and a folder of JSON.

<!-- STATS:BEGIN -->
Run `python scraper/update_readme.py` to generate this section.
<!-- STATS:END -->

## Running it

The site is entirely static, so any file server will do. It cannot be opened with
`file://` — the JSON fetches need HTTP.

```bash
git clone https://github.com/AllyOmega/ChurchFind.git
cd ChurchFind
python3 -m http.server 8000
# open http://localhost:8000
```

To deploy, point GitHub Pages at the repository root (Settings → Pages → Deploy from a
branch → `main` / `/`). Any static host works the same way — there is nothing to build.

## How the site works

The interesting constraint is size. The full dataset is tens of megabytes, which is far
too much to hand a visitor who wants to know what is down the road. So it is split:

- `data/index.json` is small and always loaded. It holds per-state counts, denomination
  totals and state centroids — enough to render the filter chips, the state grid and the
  summary numbers before any church data arrives.
- `data/states/XX.json` is fetched only when a search actually lands in that state, then
  cached in memory for the session.

Searching near a border pulls in the neighbouring states too, via an adjacency table in
`assets/js/data.js`. Without it, someone searching from Kansas City would see half a
city — the Missouri half, or the Kansas half, depending on which side of the line the
geocoder put them.

Each state file stores churches as positional arrays rather than objects, with the column
names given once in a `fields` key. Repeating sixteen key names across a hundred thousand
records costs several megabytes for nothing; the site expands rows back into objects on
load.

```jsonc
{
  "state": "CO",
  "count": 1990,
  "fields": ["id", "name", "denomination", "family", "address", "city", "state",
             "postcode", "lat", "lon", "website", "phone", "email", "services",
             "hours", "wheelchair"],
  "churches": [
    ["n358950246", "Antioch Baptist Church", "Baptist", "baptist",
     "2500 Lafayette Street", "Denver", "CO", "80205", 39.753465, -104.970488,
     "", "", "", "", "", ""]
  ]
}
```

Geocoding happens in the browser against [Nominatim](https://nominatim.org), the
OpenStreetMap geocoder. It is a free shared service with a strict rate limit — fine for
personal use, but if you put this in front of real traffic you should
[run your own instance](https://github.com/mediagis/nominatim-docker) or swap in a
commercial geocoder. Browsing by state needs no geocoder at all, which is also the
fallback the UI offers when geocoding fails.

Leaflet is vendored into `assets/vendor/leaflet/` rather than loaded from a CDN, so the
site has no third-party runtime dependency and works offline apart from map tiles. If
Leaflet or the tile host is unreachable the map pane removes itself and the results list
takes the space — the list, not the map, is the product.

Everything rendered from scraped data is built with `document.createElement` and
`textContent`, and links are scheme-checked before they reach an `href`. OpenStreetMap
tags are public input and are treated as such.

## Where the data comes from

Every record is scraped from [OpenStreetMap](https://www.openstreetmap.org) through the
[Overpass API](https://overpass-api.de). The query is, per state:

```overpassql
[out:json][timeout:300];
area["ISO3166-2"="US-CO"][admin_level=4]->.state;
(
  nwr["amenity"="place_of_worship"]["religion"="christian"](area.state);
);
out center tags;
```

`nwr` covers nodes, ways and relations, because a church may be mapped as a single point,
a building outline or a multipolygon; `out center` collapses the latter two to one
coordinate. OpenStreetMap is open data explicitly published for reuse, and Overpass is
the interface built for exactly this. No commercial church directory is touched, and
nothing is scraped from a site whose terms forbid it.

### What the scraper throws away

- **Unnamed features.** A church with no `name` tag cannot be searched for and only adds
  noise to the map.
- **Duplicates.** A church mapped as both a point and a building outline appears twice in
  the Overpass response. Records are keyed on name plus position rounded to three decimal
  places (about 100 m), and the copy carrying more detail wins.

### Known limits

OpenStreetMap is volunteer-maintained, and it shows:

- **Coverage is uneven.** Dense in cities and in states where import projects have run,
  thinner in rural counties.
- **Denomination is often blank.** It is the single largest bucket in the data — see the
  table above. A blank means nobody has tagged it, not that the congregation is
  non-denominational; the site labels these "Unspecified" and sorts the chip last.
- **Records go stale.** A congregation that has moved, merged or closed can linger for
  years. Call ahead.
- **`religion=christian` is the filter.** A place of worship with no `religion` tag is not
  included even if its name is obviously a church. That is a deliberate trade: including
  untagged places of worship would pull in mosques, synagogues and temples too.

Found something wrong? Fix it [at the source](https://www.openstreetmap.org/fixthemap) and
it flows into the next scrape. Correcting OpenStreetMap helps everyone downstream, not
just this site.

## The scraper

Python 3.8+, standard library only. No `pip install` step.

```bash
cd scraper

python3 scrape_churches.py                 # all 50 states + DC (~30-45 min)
python3 scrape_churches.py --states CO UT  # just a couple
python3 scrape_churches.py --force         # re-fetch states already on disk
python3 validate.py                        # check the generated tree
python3 update_readme.py                   # refresh the stats block above
```

Without `--force`, states already present in `data/states/` are read from disk instead of
re-fetched, so an interrupted run resumes where it stopped. `data/index.json` is rebuilt
from every state either way.

The full run takes roughly half an hour, most of it spent waiting. Overpass is a free
shared service that returns `429 Too Many Requests` and `504 Gateway Timeout` under load,
so the scraper pauses between states, retries four times with exponential backoff, and
rotates through three public mirrors. Large states sometimes need every one of those
attempts. A state that fails all four is reported at the end and left for a follow-up run
— rerun with `--states XX` to pick it up without touching the rest.

`validate.py` is the thing to run before committing or deploying. It catches the failures
that actually happen: a state file truncated by an interrupted scrape, an index whose
totals have drifted from the files it describes, coordinates that landed outside the US
after a bad parse, duplicate IDs, blank names. It exits non-zero, so it drops straight
into CI.

### Denomination normalisation

`normalize.py` maps the free-form OSM `denomination` tag onto a short list of families the
UI can offer as filter chips — `roman_catholic`, `greek_catholic` and `maronite` all land
under Catholic; `assemblies_of_god`, `foursquare` and `full_gospel` under Pentecostal &
Charismatic. Roughly ninety values are mapped explicitly. Anything unrecognised keeps its
own label (`acim` renders as "Acim") and falls into the "Other" family, so an unmapped
denomination still displays correctly rather than disappearing. Add a row to
`DENOMINATIONS` to promote one into a family.

## Keeping it fresh

`.github/workflows/refresh-data.yml` re-runs the scrape on the 1st of each month, validates
the result, and commits only if something changed. It also runs on demand from the Actions
tab. If you forked this and would rather not send that traffic at a free API, delete the
file — the site runs fine on whatever snapshot is committed.

## Layout

```
index.html                     the whole UI
assets/css/styles.css          light and dark, one set of custom properties
assets/js/data.js              state file loading, adjacency, geocoding, distance
assets/js/app.js               search, filters, list rendering, map
assets/vendor/leaflet/         vendored Leaflet 1.9.4 (BSD-2-Clause)
data/index.json                counts and centroids, always loaded
data/states/XX.json            one file per state, loaded on demand
scraper/scrape_churches.py     the Overpass scrape
scraper/normalize.py           OSM tags -> flat records, denomination mapping
scraper/states.py              state codes and centroids
scraper/validate.py            consistency checks over data/
scraper/update_readme.py       regenerates the stats block in this file
```

## Licence

Code is MIT (see [LICENSE](LICENSE)). The contents of `data/` are derived from
OpenStreetMap and carry the [Open Database License](https://opendatacommons.org/licenses/odbl/)
— if you redistribute the data or a database derived from it, attribute OpenStreetMap
contributors and keep derived databases under the same licence. Vendored Leaflet is
BSD-2-Clause.
