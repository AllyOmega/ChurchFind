# ChurchFind

A static site for finding a church near you anywhere in the United States, backed by a
scraped snapshot of OpenStreetMap.

Type a town or ZIP code, pick a denomination, drag the radius slider, and get a ranked
list with addresses, phone numbers, websites, service times and a map.

It runs two ways. Open the folder with any static file server and it works off the
bundled JSON — no build, no server, no API keys. Start the API server as well and the
same page switches to SQL over the whole country and gains accounts, so you can keep a
list of churches you care about. The page detects which it has and says so.

<!-- STATS:BEGIN -->

**235,784 churches** across **50 states and DC**, from an OpenStreetMap snapshot taken **2026-08-02**.

| | Count | Share |
|---|---:|---:|
| Has a street address | 70,740 | 30% |
| Has a website | 26,452 | 11% |
| Has a phone number | 22,180 | 9% |

Denominational families, largest first:

| Family | Churches | Share |
|---|---:|---:|
| Unspecified | 127,658 | 54.1% |
| Baptist | 38,480 | 16.3% |
| Methodist & Wesleyan | 15,591 | 6.6% |
| Catholic | 13,945 | 5.9% |
| Lutheran | 9,037 | 3.8% |
| Restorationist | 8,693 | 3.7% |
| Presbyterian & Reformed | 7,335 | 3.1% |
| Pentecostal & Charismatic | 4,798 | 2.0% |
| Other | 2,823 | 1.2% |
| Anglican & Episcopal | 2,815 | 1.2% |
| Non-denominational | 2,105 | 0.9% |
| Orthodox | 1,711 | 0.7% |
| Anabaptist & Peace Churches | 793 | 0.3% |

Ten largest state files:

| State | Churches |
|---|---:|
| Texas | 16,251 |
| Georgia | 12,320 |
| Alabama | 11,787 |
| North Carolina | 11,675 |
| California | 11,661 |
| Tennessee | 10,340 |
| Ohio | 9,809 |
| Pennsylvania | 9,370 |
| Virginia | 9,124 |
| Illinois | 9,111 |

<!-- STATS:END -->

## Running it

### Static, no server

Any file server will do. It cannot be opened with `file://` — the JSON fetches need HTTP.

```bash
git clone https://github.com/AllyOmega/ChurchFind.git
cd ChurchFind
python3 -m http.server 8000
# open http://localhost:8000
```

To deploy this way, point GitHub Pages at the repository root (Settings → Pages → Deploy
from a branch → `main` / `/`). Any static host works the same — there is nothing to build.
Accounts are unavailable, and the page says so in a banner rather than leaving you to
wonder where the sign-in button went.

### With the API server

```bash
pip install fastapi "uvicorn[standard]" argon2-cffi
python server/build_db.py                      # data/ -> SQLite, about a minute
CHURCHFIND_INSECURE_COOKIES=1 \
  uvicorn server.app:app --port 8000           # drop that variable behind HTTPS
```

The server hosts the site as well as the API, so there is still one thing to open. The
database is a build artifact — it is rebuilt from `data/`, and is not in the repository.

**`CHURCHFIND_INSECURE_COOKIES=1` is for local http only.** Session cookies are marked
`Secure` by default, and a browser will not send a `Secure` cookie over plain http, so
sign-in silently fails without it. Behind HTTPS, leave it unset. The name is deliberately
unpleasant so it does not survive a copy-paste into production.

## How the site works

Search goes through one interface with two backends behind it. With the API server
running, a search is a SQL query over the whole country. Without it, the same search runs
over per-state JSON files loaded into memory. The page picks by probing `/api/health` at
boot; nothing else in the UI knows which it got, except that accounts only exist in the
first case. Both paths are tested against each other and return identical counts.

The rest of this section is about the static path, where the interesting constraint is
size. The full dataset is tens of megabytes, far too much to hand a visitor who wants to
know what is down the road. So it is split:

- `data/index.json` is small and always loaded. It holds per-state counts, denomination
  totals and state centroids — enough to render the filter chips, the state grid and the
  summary numbers before any church data arrives.
- `data/states/XX.json` is fetched only when a search actually lands in that state, then
  cached in memory for the session.

Searching near a border needs the neighbouring states too — without them, someone
searching from Kansas City sees half a city, the Missouri half or the Kansas half
depending on which side of the line the geocoder put them. But Missouri touches eight
states, and blocking the first result on nine files is worse than the problem it solves.
So the home state renders immediately and the neighbours, listed in an adjacency table in
`assets/js/data.js`, fold in when they arrive. Each search carries a token so a slow load
from an abandoned search cannot leak into the current one.

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

## Accounts and the database

`server/build_db.py` loads `data/states/*.json` into SQLite. The church tables are
rebuilt wholesale each time — they are a cache of the scrape, not a system of record —
while the account tables are left alone, so importing a fresh snapshot never touches
anyone's login or saved list.

Two things make the query side worth having. `churches_geo` is an R\*Tree over the
coordinates, so a radius search narrows to a bounding box before a single haversine runs;
`churches_fts` is an FTS5 index over name, city and denomination. Across 235,783 rows on
a laptop:

| Query | Results | Time |
|---|---:|---:|
| Everything within 25 miles of Denver | 873 | 7 ms |
| …narrowed to the Catholic family | 100 | 4 ms |
| 6 miles around Kansas City, crossing the state line | 333 | 3 ms |
| `grace` anywhere in the country | 3,433 | 8 ms |
| Every church in Texas | 16,239 | 4 ms |

Which is the real reason the server exists: the static build has to download a whole
state to filter it, and cannot answer "grace, anywhere" at all.

### What is stored

| Table | Holds |
|---|---|
| `churches` + `churches_geo` + `churches_fts` | the scrape, read-only at runtime |
| `users` | email, argon2id hash, display name, optional home location |
| `sessions` | SHA-256 of the cookie token, CSRF digest, expiry, user agent |
| `saved_churches` | user → church, with a private note |
| `correction_reports` | queue for a human; the fix belongs upstream in OSM |
| `auth_attempts` | throttle history, pruned after 24 hours |

### The security decisions

These are the parts worth arguing with, so here is the reasoning rather than a checklist.

- **Passwords use argon2id** with the library's defaults, and `needs_rehash` on every
  login means the cost can be raised later without forcing a reset.
- **The session token is never stored.** The cookie holds 256 bits from `secrets`; the
  database holds its SHA-256. Reading the `sessions` table gives an attacker nothing they
  can put in a cookie. A slow KDF would be pointless here — the input is already random,
  so there is nothing to guess.
- **Two cookies, on purpose.** `cf_session` is `HttpOnly`, so no script can read it, and
  it is the one that authenticates. `cf_csrf` is deliberately readable, because the page
  has to echo it back in an `X-CSRF-Token` header on every state-changing request. A
  cross-site form can make the browser send cookies but cannot read one to build the
  header — that asymmetry is the whole mechanism. Both are `SameSite=Lax` and `Secure`.
- **Login does not confirm whether an address exists.** Wrong password and unknown email
  return the same status and the same sentence, and an unknown email still pays for a
  hash so the timing matches. Registration cannot hide it — the account has to be unique
  — but registration is not where enumeration is useful.
- **Throttling is per-email and per-address**, so one account cannot be ground down and
  one host cannot spray many accounts. A successful login clears the account's counter.
- **Changing a password destroys every session**, which is the entire point of changing it
  after a scare.
- **Every query is parameterised.** The only strings interpolated into SQL are column
  names and placeholder lists the code writes itself. User text also never reaches FTS5's
  query syntax raw: punctuation is stripped and each term quoted, because `NEAR/2` and a
  bare `*` are operators there, not searches.
- **A tight CSP**, with no `unsafe-inline` — every script and style is a file. The only
  cross-origin destinations allowed are OSM tiles and the geocoder.

`server/test_api.py` covers this: 48 tests, including that the stored hash is not the
cookie, that a session cookie without the CSRF header is refused, that two users cannot
see each other's saved churches, that `'; DROP TABLE churches; --` and five other hostile
strings leave the table standing, and that an expired cookie cannot be replayed.

```bash
cd server && python -m pytest test_api.py -q
```

### What is deliberately missing

Worth knowing before you put this in front of anyone:

- **No email verification and no password reset.** The server sends no email at all, so a
  forgotten password is a lost account. This is the first thing to add.
- **No admin interface.** `correction_reports` fills up and nothing reads it.
- **The common-password list is a token gesture** — sixteen entries. A real deployment
  should check Pwned Passwords by k-anonymity, or ship a local bloom filter.
- **Throttling lives in the application**, so it counts per process and trusts
  `request.client.host`. Behind a proxy that needs to become the forwarded address, and
  it belongs at the edge as well.
- **SQLite with one shared connection** is right for one process and a read-mostly
  workload. Concurrent writers want Postgres.

### API

| | |
|---|---|
| `GET /api/health` | is the server up, how many churches |
| `GET /api/meta` | families, the denominations inside each, states |
| `GET /api/churches` | `lat` `lon` `radius` `state` `q` `family` `denomination` `has_website` `has_phone` `has_services` `wheelchair` `sort` `limit` `offset` |
| `GET /api/churches/{id}` | one church, plus whether you saved it |
| `POST /api/auth/register` · `login` · `logout` · `password` | accounts |
| `GET /api/auth/me` | current user, or `null` |
| `GET` `POST` `/api/saved`, `DELETE /api/saved/{id}` | saved churches |
| `PUT /api/me/home` | home location |
| `POST /api/reports` | suggest a correction |

Interactive docs at `/api/docs`.

## Filtering by denomination

The filter is two levels, because one is not enough at either end. There are 11
denominational families and 481 distinct denominations, and 54% of records have no
denomination tagged at all.

Picking a family — Baptist, Catholic, Orthodox — filters immediately and reveals a second
control listing only the specific denominations inside the families you picked, with
counts. Choose "Roman Catholic" and a Denver search goes from 100 Catholic churches to
the 91 tagged specifically. Each choice becomes a removable pill, and families combine
with each other and with everything else in the panel.

A flat list of 481 would be unusable, and families alone cannot tell Southern Baptist
from Free Will Baptist. The nesting comes from `normalize.py`, which maps roughly ninety
raw OSM values onto families; anything unrecognised keeps its own label and lands in
"Other" rather than disappearing. Both the API and the static build produce identical
results — the two code paths are checked against each other.

## What I would build next

Ordered by what the data already supports.

**Cheap, and the data is already there**
- **Service-time search** — "somewhere with a Sunday evening service". `service_times` is
  populated on a slice of records and OSM's `opening_hours` grammar is parseable.
- **Stale-record flags.** OSM exposes a last-edited timestamp per feature. A church
  untouched since 2013 deserves a quieter presentation than one edited last month.
- **State and metro pages** — real URLs like `/tx/austin`, which is also the only way any
  of this gets indexed by a search engine. Today everything is one page and a query string.
- **Fuzzy-duplicate detection.** The scraper only merges exact name matches at the same
  spot; "St Mary's" and "Saint Mary's" 40 m apart are still two records.

**Now that accounts exist**
- **Password reset and email verification**, as above — the gap that matters most.
- **A moderation queue** for `correction_reports`, with an admin role.
- **Notes and visit history** — the `note` column exists and only the API uses it.
- **"New near home"** — a monthly digest when churches appear near a saved home location.
  The scrape already runs monthly, so the diff is free.

**Bigger**
- **Search along a route**, for moving or travelling rather than standing still.
- **Claimed listings.** Let a congregation verify itself and correct its own entry, with
  the changes pushed back to OpenStreetMap so everyone downstream benefits.
- **Accessibility beyond `wheelchair=yes`** — hearing loops, step-free access, parking.
  These are real OSM tags that this scrape currently discards.

Two I would push back on. **Public reviews of congregations** would need moderation far
beyond what a side project can staff, and the failure mode is ugly. **Attendance or
"popularity" figures** are not in the data and cannot be estimated from it honestly.

## Keeping it fresh

`.github/workflows/refresh-data.yml` re-runs the scrape on the 1st of each month, validates
the result, and commits only if something changed. It also runs on demand from the Actions
tab. If you forked this and would rather not send that traffic at a free API, delete the
file — the site runs fine on whatever snapshot is committed.

## Layout

```
index.html                     the whole UI
assets/css/styles.css          light and dark, one set of custom properties
assets/js/data.js              one search interface over the API and the static files
assets/js/account.js           auth calls, the sign-in dialog, saved churches
assets/js/app.js               search, filters, list rendering, map
assets/vendor/leaflet/         vendored Leaflet 1.9.4 (BSD-2-Clause)
data/index.json                counts, denominations and centroids, always loaded
data/states/XX.json            one file per state, loaded on demand
scraper/scrape_churches.py     the Overpass scrape
scraper/normalize.py           OSM tags -> flat records, denomination and state mapping
scraper/states.py              state codes and centroids
scraper/validate.py            consistency checks over data/
scraper/update_readme.py       regenerates the stats block in this file
server/schema.sql              the database, churches and accounts
server/db.py                   connections, pragmas, haversine as a SQL function
server/build_db.py             data/ -> SQLite
server/queries.py              church search: R*Tree radius, FTS5 names
server/auth.py                 argon2id, sessions, CSRF, throttling
server/app.py                  FastAPI routes and the static host
server/test_api.py             48 tests, weighted towards the security-critical parts
```

## Licence

Code is MIT (see [LICENSE](LICENSE)). The contents of `data/` are derived from
OpenStreetMap and carry the [Open Database License](https://opendatacommons.org/licenses/odbl/)
— if you redistribute the data or a database derived from it, attribute OpenStreetMap
contributors and keep derived databases under the same licence. Vendored Leaflet is
BSD-2-Clause.
