-- ChurchFind database schema.
--
-- Two halves that barely touch: scraped church data, rebuilt wholesale from
-- data/ and read-only at runtime, and account data, which is the only thing in
-- here that would hurt if it leaked.
--
-- Every text column is NOT NULL with a default so the application never has to
-- distinguish "missing" from "empty" -- OpenStreetMap gives us plenty of empty
-- and no NULLs.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- church data

CREATE TABLE IF NOT EXISTS churches (
  id            TEXT PRIMARY KEY,              -- OSM identity, e.g. n358950246
  name          TEXT NOT NULL,
  denomination  TEXT NOT NULL DEFAULT '',      -- display label, "Southern Baptist"
  family        TEXT NOT NULL,                 -- filter bucket, "baptist"
  address       TEXT NOT NULL DEFAULT '',
  city          TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL,
  postcode      TEXT NOT NULL DEFAULT '',
  lat           REAL NOT NULL,
  lon           REAL NOT NULL,
  website       TEXT NOT NULL DEFAULT '',
  phone         TEXT NOT NULL DEFAULT '',
  email         TEXT NOT NULL DEFAULT '',
  services      TEXT NOT NULL DEFAULT '',
  hours         TEXT NOT NULL DEFAULT '',
  wheelchair    TEXT NOT NULL DEFAULT '',
  hearing_loop  TEXT NOT NULL DEFAULT '',
  toilets_wheelchair TEXT NOT NULL DEFAULT '',
  updated       TEXT NOT NULL DEFAULT '',   -- OSM last-edit date, YYYY-MM-DD
  wikipedia     TEXT NOT NULL DEFAULT '',   -- OSM wikipedia tag, e.g. "en:Title"
  wikidata      TEXT NOT NULL DEFAULT '',   -- OSM wikidata tag, e.g. "Q12345"
  -- Parsed out of `services` at build time by scraper/service_times.py.
  -- Day and time together as ",6-0900,3-1900," -- see scraper/service_times.py
  -- on why they are pairs rather than two independent sets.
  service_pairs TEXT NOT NULL DEFAULT '',
  service_text  TEXT NOT NULL DEFAULT '',   -- human-readable rendering
  -- Where the times came from: 'osm' (a service_times tag) or 'website' (read
  -- off the parish's own page). A time nobody can trace is a time nobody can
  -- correct, and turning up to a locked door is the cost of getting it wrong.
  service_source TEXT NOT NULL DEFAULT '',
  -- When a website-derived time was read, YYYY-MM-DD. A scraped schedule goes
  -- stale the first time a parish changes its summer hours without telling
  -- anyone, and a time with no date on it cannot be judged at all.
  service_checked TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_churches_state  ON churches(state);
CREATE INDEX IF NOT EXISTS idx_churches_family ON churches(family);
CREATE INDEX IF NOT EXISTS idx_churches_denom  ON churches(denomination);

-- Partial indexes for the "only show" toggles, keyed on state because the
-- toggles are nearly always combined with a state browse. Only ~11% of rows have
-- a website and ~9% a phone, so these stay small.
CREATE INDEX IF NOT EXISTS idx_churches_website  ON churches(state) WHERE website  <> '';
CREATE INDEX IF NOT EXISTS idx_churches_phone    ON churches(state) WHERE phone    <> '';
CREATE INDEX IF NOT EXISTS idx_churches_services ON churches(state) WHERE services <> '';
CREATE INDEX IF NOT EXISTS idx_churches_svcpairs ON churches(state) WHERE service_pairs <> '';

-- Radius search. Points are stored as degenerate boxes; the R-tree narrows to a
-- bounding box and exact haversine runs over what survives.
CREATE VIRTUAL TABLE IF NOT EXISTS churches_geo USING rtree(
  id, min_lat, max_lat, min_lon, max_lon
);

-- Name search. External-content FTS over churches; no sync triggers because
-- the application never writes to churches -- build_db.py rebuilds both.
CREATE VIRTUAL TABLE IF NOT EXISTS churches_fts USING fts5(
  name, city, denomination,
  content='churches', content_rowid='rowid', tokenize='unicode61'
);

-- Provenance for the snapshot currently loaded.
CREATE TABLE IF NOT EXISTS dataset_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ------------------------------------------------------------------- accounts

CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY,
  email          TEXT NOT NULL UNIQUE,     -- normalised to lowercase before insert
  password_hash  TEXT NOT NULL,            -- argon2id, includes its own salt and params
  display_name   TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL,
  last_login_at  TEXT,
  home_lat       REAL,
  home_lon       REAL,
  home_label     TEXT NOT NULL DEFAULT '',
  is_active      INTEGER NOT NULL DEFAULT 1,
  is_moderator   INTEGER NOT NULL DEFAULT 0
);

-- Sessions hold the SHA-256 of the cookie token, never the token. Someone who
-- reads this table cannot mint a cookie from it.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash    TEXT PRIMARY KEY,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_hash     TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  user_agent    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS saved_churches (
  user_id     INTEGER NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
  church_id   TEXT    NOT NULL REFERENCES churches(id) ON DELETE CASCADE,
  note        TEXT    NOT NULL DEFAULT '',
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (user_id, church_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_churches(user_id, created_at DESC);

-- Corrections go to OpenStreetMap, not into churches -- see the README. This is
-- a queue for a human, not an edit path.
CREATE TABLE IF NOT EXISTS correction_reports (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  church_id   TEXT NOT NULL REFERENCES churches(id) ON DELETE CASCADE,
  field       TEXT NOT NULL,
  suggestion  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_status ON correction_reports(status, created_at DESC);

-- Password reset. The token is stored as a SHA-256 the same way session tokens
-- are: somebody who reads this table cannot mint a working link from it.
--
-- `used_at` rather than deleting the row on use, so a second click on the same
-- link is distinguishable from a link that never existed, and so a burst of
-- resets is visible when something is wrong.
CREATE TABLE IF NOT EXISTS password_resets (
  token_hash  TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  used_at     TEXT NOT NULL DEFAULT '',
  requested_ip TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_resets_user    ON password_resets(user_id);
CREATE INDEX IF NOT EXISTS idx_resets_expires ON password_resets(expires_at);

-- Throttling for login and registration. Rows are pruned as they age out.
CREATE TABLE IF NOT EXISTS auth_attempts (
  id       INTEGER PRIMARY KEY,
  bucket   TEXT NOT NULL,        -- "login:<email>", "login:ip:<addr>", "register:ip:<addr>"
  at       TEXT NOT NULL,
  success  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_attempts_bucket ON auth_attempts(bucket, at);

-- ------------------------------------------------------------------- reviews

-- Reviews are the one place a user's own words become public, so nothing here
-- is published without passing moderation. `status` is the gate; the default is
-- deliberately `pending`, so a row that somehow skips the moderation path is
-- invisible rather than live.
CREATE TABLE IF NOT EXISTS reviews (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
  church_id     TEXT    NOT NULL REFERENCES churches(id) ON DELETE CASCADE,
  rating        INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  body          TEXT    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'escalated')),
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  -- Moderation record. Kept alongside the review so a human reviewing the queue
  -- can see what the model decided and why, and so a bad call is auditable.
  mod_model     TEXT    NOT NULL DEFAULT '',
  mod_verdict   TEXT    NOT NULL DEFAULT '',
  mod_reason    TEXT    NOT NULL DEFAULT '',
  mod_categories TEXT   NOT NULL DEFAULT '',   -- comma-separated
  mod_at        TEXT    NOT NULL DEFAULT '',
  -- Set when a human overrides the model, so the two are never confused.
  decided_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  decided_at    TEXT    NOT NULL DEFAULT '',
  UNIQUE (user_id, church_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_church ON reviews(church_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_queue  ON reviews(status, created_at) WHERE status IN ('pending', 'escalated');

-- Moderators are flagged on the user row rather than in a roles table -- there
-- are exactly two levels and no prospect of a third.

-- ------------------------------------------------------------ churchmanship

-- Two-axis estimate for Anglican parishes. The scraped_* columns are what the
-- website analysis produced; the plain columns are that blended with user
-- submissions. Keeping both means a bad scrape can be re-run without losing
-- votes, and a moderator can see which part of the number is whose.
CREATE TABLE IF NOT EXISTS churchmanship (
  church_id          TEXT PRIMARY KEY REFERENCES churches(id) ON DELETE CASCADE,
  ceremonial         REAL,
  theology           REAL,
  confidence         REAL NOT NULL DEFAULT 0,
  votes              INTEGER NOT NULL DEFAULT 0,
  source             TEXT NOT NULL DEFAULT 'estimated',
  scraped_ceremonial REAL,
  scraped_theology   REAL,
  scraped_confidence REAL NOT NULL DEFAULT 0,
  -- Which scrape produced it: 'website', 'wikipedia', or 'website+wikipedia'.
  -- Shown to the reader, because "the parish says so" and "an encyclopedia
  -- article about the building says so" are not the same claim.
  scraped_source     TEXT NOT NULL DEFAULT '',
  evidence           TEXT NOT NULL DEFAULT '',   -- comma-separated matched phrases
  updated_at         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS churchmanship_votes (
  user_id     INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
  church_id   TEXT    NOT NULL REFERENCES churches(id) ON DELETE CASCADE,
  ceremonial  REAL    NOT NULL CHECK (ceremonial BETWEEN -1 AND 1),
  theology    REAL    NOT NULL CHECK (theology   BETWEEN -1 AND 1),
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (user_id, church_id)
);

CREATE INDEX IF NOT EXISTS idx_cmvotes_church ON churchmanship_votes(church_id);
