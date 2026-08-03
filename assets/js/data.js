/* Data access for ChurchFind.
 *
 * Two backends behind one interface. When the API server is running, searches
 * are SQL over the whole country. When it is not -- a GitHub Pages deploy, or a
 * clone opened with `python -m http.server` -- the same searches run against
 * per-state JSON files loaded into memory. The page cannot tell the difference
 * except that accounts only exist in the first case.
 */
(function (global) {
  'use strict';

  var INDEX_URL = 'data/index.json';
  var STATE_URL = 'data/states/';
  var API = 'api';
  var NOMINATIM = 'https://nominatim.openstreetmap.org';

  // OpenStreetMap's public tiles are fine for local use and small deployments,
  // but their usage policy rules out anything with real traffic. The server can
  // override this with CHURCHFIND_TILE_URL; it also has to appear in the CSP,
  // which is why the server owns the value rather than the page hardcoding it.
  var DEFAULT_TILES = {
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  };
  var tiles = DEFAULT_TILES;
  var EARTH_RADIUS_MILES = 3958.8;

  var ADJACENT = {
    AL: ['FL', 'GA', 'MS', 'TN'],
    AK: [],
    AZ: ['CA', 'NV', 'UT', 'CO', 'NM'],
    AR: ['MO', 'TN', 'MS', 'LA', 'TX', 'OK'],
    CA: ['OR', 'NV', 'AZ'],
    CO: ['WY', 'NE', 'KS', 'OK', 'NM', 'AZ', 'UT'],
    CT: ['NY', 'MA', 'RI'],
    DE: ['MD', 'PA', 'NJ'],
    DC: ['MD', 'VA'],
    FL: ['GA', 'AL'],
    GA: ['FL', 'AL', 'TN', 'NC', 'SC'],
    HI: [],
    ID: ['WA', 'OR', 'NV', 'UT', 'WY', 'MT'],
    IL: ['WI', 'IA', 'MO', 'KY', 'IN'],
    IN: ['MI', 'OH', 'KY', 'IL'],
    IA: ['MN', 'WI', 'IL', 'MO', 'NE', 'SD'],
    KS: ['NE', 'MO', 'OK', 'CO'],
    KY: ['IN', 'OH', 'WV', 'VA', 'TN', 'MO', 'IL'],
    LA: ['TX', 'AR', 'MS'],
    ME: ['NH'],
    MD: ['VA', 'WV', 'PA', 'DE', 'DC'],
    MA: ['RI', 'CT', 'NY', 'VT', 'NH'],
    MI: ['OH', 'IN', 'WI'],
    MN: ['WI', 'IA', 'SD', 'ND'],
    MS: ['LA', 'AR', 'TN', 'AL'],
    MO: ['IA', 'IL', 'KY', 'TN', 'AR', 'OK', 'KS', 'NE'],
    MT: ['ND', 'SD', 'WY', 'ID'],
    NE: ['SD', 'IA', 'MO', 'KS', 'CO', 'WY'],
    NV: ['OR', 'ID', 'UT', 'AZ', 'CA'],
    NH: ['VT', 'ME', 'MA'],
    NJ: ['NY', 'PA', 'DE'],
    NM: ['AZ', 'UT', 'CO', 'OK', 'TX'],
    NY: ['NJ', 'PA', 'CT', 'MA', 'VT'],
    NC: ['VA', 'TN', 'GA', 'SC'],
    ND: ['MN', 'SD', 'MT'],
    OH: ['PA', 'WV', 'KY', 'IN', 'MI'],
    OK: ['KS', 'MO', 'AR', 'TX', 'NM', 'CO'],
    OR: ['WA', 'ID', 'NV', 'CA'],
    PA: ['NY', 'NJ', 'DE', 'MD', 'WV', 'OH'],
    RI: ['CT', 'MA'],
    SC: ['NC', 'GA'],
    SD: ['ND', 'MN', 'IA', 'NE', 'WY', 'MT'],
    TN: ['KY', 'VA', 'NC', 'GA', 'AL', 'MS', 'AR', 'MO'],
    TX: ['NM', 'OK', 'AR', 'LA'],
    UT: ['ID', 'WY', 'CO', 'NM', 'AZ', 'NV'],
    VT: ['NY', 'NH', 'MA'],
    VA: ['NC', 'TN', 'KY', 'WV', 'MD', 'DC'],
    WA: ['ID', 'OR'],
    WV: ['OH', 'PA', 'MD', 'VA', 'KY'],
    WI: ['MI', 'MN', 'IA', 'IL'],
    WY: ['MT', 'SD', 'NE', 'CO', 'UT', 'ID']
  };

  // Must stay in step with scraper/service_times.py PERIODS.
  var SERVICE_PERIODS = {
    early: [0, 480], morning: [480, 720], midday: [720, 900],
    afternoon: [900, 1080], evening: [1080, 1440]
  };

  var mode = null;              // 'api' or 'static', decided by init()
  var metaCache = null;
  var stateCache = {};
  var pool = [];                // static mode only: the churches currently in memory
  var poolIds = {};
  var nameToCode = null;

  /* ------------------------------------------------------------------- boot */

  function init() {
    // A short timeout so a Pages deploy, where /api/health is a 404 served as
    // index.html, falls back promptly instead of hanging on the probe.
    return fetch(API + '/health', { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) throw new Error('no api');
        return response.json();
      })
      .then(function (health) {
        if (!health || health.ok !== true) throw new Error('no api');
        mode = 'api';
        return loadApiMeta();
      })
      .catch(function () {
        mode = 'static';
        return loadStaticMeta();
      })
      .then(function (meta) {
        metaCache = meta;
        if (meta.tiles && meta.tiles.url) tiles = meta.tiles;
        return { mode: mode, meta: meta };
      });
  }

  function currentMode() { return mode; }
  function hasAccounts() { return mode === 'api'; }

  /* Both backends return the same shape: families with counts, the specific
     denominations inside each, states, and the dataset stamp. */
  function loadApiMeta() {
    return fetch(API + '/meta').then(function (response) {
      if (!response.ok) throw new Error('Could not load the church index.');
      return response.json();
    }).then(function (body) {
      return {
        total: body.total,
        families: body.families,
        denominations: body.denominations,
        states: body.states,
        tiles: body.tiles,
        servicePeriods: body.servicePeriods,
        withServiceTimes: body.withServiceTimes,
        dataset: body.dataset || {}
      };
    });
  }

  function loadStaticMeta() {
    return fetch(INDEX_URL).then(function (response) {
      if (!response.ok) throw new Error('Could not load the church index (' + response.status + ').');
      return response.json();
    }).then(function (index) {
      var families = Object.keys(index.families).map(function (key) {
        return { key: key, label: index.families[key].label, count: index.families[key].count };
      });
      return {
        total: index.totals.churches,
        families: families,
        denominations: index.denominations || {},
        states: index.states,
        totals: index.totals,
        dataset: {
          osm_snapshot: index.osm_snapshot,
          generated_at: index.generated_at
        }
      };
    });
  }

  function meta() { return metaCache; }

  /* -------------------------------------------------- static-mode pool loading */

  // Churchmanship for the no-server build. The API gets these from a LEFT JOIN;
  // without one the state files carry no scores at all, so the whole feature was
  // invisible on the deployed site. 446 readings is a small file, fetched once
  // and merged onto churches as they load, so both backends hand the UI the same
  // cm_* fields and nothing downstream has to know which mode it is in.
  var CHURCHMANSHIP_URL = 'data/churchmanship-merged.json';
  var churchmanship = null;          // id -> reading, once loaded
  var churchmanshipLoad = null;

  function loadChurchmanship() {
    if (churchmanshipLoad) return churchmanshipLoad;
    churchmanshipLoad = fetch(CHURCHMANSHIP_URL)
      .then(function (response) { return response.ok ? response.json() : { scores: {} }; })
      // A missing or broken file means no readings, never a broken search.
      .catch(function () { return { scores: {} }; })
      .then(function (payload) { churchmanship = payload.scores || {}; return churchmanship; });
    return churchmanshipLoad;
  }

  function attachChurchmanship(church) {
    var reading = churchmanship && churchmanship[church.id];
    if (!reading) return church;
    church.cm_ceremonial = reading.ceremonial;
    church.cm_theology = reading.theology;
    church.cm_confidence = reading.confidence;
    church.cm_scraped_source = reading.source;
    church.cm_evidence = (reading.evidence || []).join(',');
    church.cm_votes = 0;             // votes need accounts, which need the server
    church.cm_source = 'estimated';
    return church;
  }

  function expand(payload) {
    var fields = payload.fields;
    return payload.churches.map(function (row) {
      var church = {};
      for (var i = 0; i < fields.length; i++) church[fields[i]] = row[i];
      return attachChurchmanship(church);
    });
  }

  function loadState(code) {
    code = String(code).toUpperCase();
    if (!stateCache[code]) {
      // The readings have to be in hand before expand() runs, or the first
      // state loaded would come back without them.
      stateCache[code] = loadChurchmanship()
        .then(function () { return fetch(STATE_URL + code + '.json'); })
        .then(function (response) {
          if (!response.ok) throw new Error('No data file for ' + code + '.');
          return response.json();
        })
        .then(expand)
        .catch(function (error) {
          delete stateCache[code];   // let a later attempt retry rather than cache the failure
          throw error;
        });
    }
    return stateCache[code];
  }

  function addToPool(churches) {
    churches.forEach(function (church) {
      // A church on a state line is in both states' files. Merging blind would
      // list it twice, which is exactly the border case neighbours exist for.
      if (poolIds[church.id]) return;
      poolIds[church.id] = true;
      pool.push(church);
    });
  }

  function resetPool() {
    pool = [];
    poolIds = {};
  }

  /* Load whatever the next search needs. In API mode there is nothing to load. */
  function prepare(stateCode) {
    if (mode === 'api') return Promise.resolve();
    resetPool();
    return loadState(stateCode).then(addToPool);
  }

  /* Neighbouring states, so a border search isn't cut in half. Kept separate
     from prepare() because Missouri touches eight states and nobody should wait
     on nine files to see their first result. */
  function expandRegion(stateCode) {
    if (mode === 'api') return Promise.resolve(false);
    var codes = ADJACENT[String(stateCode).toUpperCase()] || [];
    if (!codes.length) return Promise.resolve(false);
    return Promise.all(codes.map(function (code) {
      return loadState(code).catch(function () { return []; });
    })).then(function (lists) {
      var before = pool.length;
      lists.forEach(addToPool);
      return pool.length > before;
    });
  }

  /* ----------------------------------------------------------------- searching */

  function search(params) {
    return mode === 'api' ? searchApi(params) : Promise.resolve(searchStatic(params));
  }

  /* One church by id, for the detail view and for deep links.
   *
   * Static mode needs the state code as well: an OSM id says nothing about
   * where it is, and the data is split into one file per state, so without it
   * there is no way to know which of 51 files to open. The API has an index and
   * does not care -- it ignores the second argument.
   */
  function getChurch(id, stateCode) {
    if (mode === 'api') {
      return fetch(API + '/churches/' + encodeURIComponent(id)).then(function (response) {
        if (response.status === 404) return null;
        if (!response.ok) throw new Error('Could not load that church.');
        return response.json();
      });
    }
    var known = poolIds[id] && pool.filter(function (c) { return c.id === id; })[0];
    if (known) return Promise.resolve(known);
    if (!stateCode) return Promise.resolve(null);
    return loadState(stateCode).then(function (churches) {
      for (var i = 0; i < churches.length; i++) {
        if (churches[i].id === id) return churches[i];
      }
      return null;
    });
  }

  function searchApi(params) {
    var query = new URLSearchParams();
    if (params.lat != null && params.lon != null) {
      query.set('lat', params.lat);
      query.set('lon', params.lon);
      query.set('radius', params.radius);
    }
    if (params.state) query.set('state', params.state);
    if (params.q) query.set('q', params.q);
    if (params.families && params.families.length) query.set('family', params.families.join(','));
    if (params.denominations && params.denominations.length) {
      query.set('denomination', params.denominations.join(','));
    }
    if (params.serviceDays && params.serviceDays.length) {
      query.set('service_days', params.serviceDays.join(','));
    }
    if (params.servicePeriods && params.servicePeriods.length) {
      query.set('service_periods', params.servicePeriods.join(','));
    }
    ['hasWebsite', 'hasPhone', 'hasServices', 'wheelchair', 'hearingLoop'].forEach(function (key) {
      if (params[key]) query.set(key.replace(/[A-Z]/g, function (c) { return '_' + c.toLowerCase(); }), 'true');
    });
    query.set('sort', params.sort || 'distance');
    query.set('limit', params.limit || 50);
    query.set('offset', params.offset || 0);

    return fetch(API + '/churches?' + query.toString()).then(function (response) {
      if (!response.ok) throw new Error('Search failed (' + response.status + ').');
      return response.json();
    });
  }

  /* Static mode has no parsed service columns -- the JSON files carry the raw
     OSM tag. Rather than ship a second parser to the browser, the static build
     matches on the same comma-wrapped strings the server derives, which the
     scraper now writes into index.json per state. When they are absent the
     filters simply do not appear (see renderServiceFilters). */
  function matchesServiceTimes(church, params) {
    var days = params.serviceDays || [];
    var periods = params.servicePeriods || [];
    if (!days.length && !periods.length) return true;

    var pairs = church.service_pairs || '';
    if (!pairs) return false;

    var hours = [];
    periods.forEach(function (period) {
      var window = SERVICE_PERIODS[period];
      if (!window) return;
      for (var h = Math.floor(window[0] / 60); h <= Math.floor((window[1] - 1) / 60); h++) {
        hours.push(h < 10 ? '0' + h : String(h));
      }
    });

    // Day + period together means "a service at that time on that day", which
    // is only answerable because the pairs were kept together.
    if (days.length && hours.length) {
      return days.some(function (day) {
        return hours.some(function (hour) { return pairs.indexOf(',' + day + '-' + hour) !== -1; });
      });
    }
    if (days.length) {
      return days.some(function (day) { return pairs.indexOf(',' + day + '-') !== -1; });
    }
    return hours.some(function (hour) { return pairs.indexOf('-' + hour) !== -1; });
  }

  function searchStatic(params) {
    var families = params.families || [];
    var denominations = params.denominations || [];
    var needle = (params.q || '').trim().toLowerCase();
    var near = params.lat != null && params.lon != null;

    var matched = [];
    for (var i = 0; i < pool.length; i++) {
      var church = pool[i];

      if (params.state && church.state !== params.state) continue;
      if (families.length && families.indexOf(church.family) === -1) continue;
      if (denominations.length && denominations.indexOf(church.denomination) === -1) continue;
      if (needle && church.name.toLowerCase().indexOf(needle) === -1) continue;
      if (params.hasWebsite && !church.website) continue;
      if (params.hasPhone && !church.phone) continue;
      if (params.hasServices && !church.services) continue;
      if (params.wheelchair && church.wheelchair !== 'yes') continue;
      if (params.hearingLoop && church.hearing_loop !== 'yes') continue;
      if (!matchesServiceTimes(church, params)) continue;

      var distance = near ? haversine(params.lat, params.lon, church.lat, church.lon) : null;
      if (near && distance > params.radius) continue;

      var copy = Object.create(church);
      copy.distance = distance;
      matched.push(copy);
    }

    var sort = params.sort || 'distance';
    if (!near && sort === 'distance') sort = 'name';
    matched.sort(function (a, b) {
      if (sort === 'name') return a.name.localeCompare(b.name);
      if (sort === 'denomination') {
        var left = a.denomination || '￿';
        var right = b.denomination || '￿';
        return left.localeCompare(right) || a.name.localeCompare(b.name);
      }
      return a.distance - b.distance;
    });

    var offset = params.offset || 0;
    var limit = params.limit || 50;
    return { total: matched.length, results: matched.slice(offset, offset + limit) };
  }

  /* ------------------------------------------------------------------- geo */

  function haversine(lat1, lon1, lat2, lon2) {
    var toRad = Math.PI / 180;
    var dLat = (lat2 - lat1) * toRad;
    var dLon = (lon2 - lon1) * toRad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return EARTH_RADIUS_MILES * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function buildNameLookup() {
    if (nameToCode) return nameToCode;
    nameToCode = {};
    (metaCache.states || []).forEach(function (state) {
      nameToCode[state.name.toLowerCase()] = state.code;
      nameToCode[state.code.toLowerCase()] = state.code;
    });
    return nameToCode;
  }

  function stateCodeFromAddress(address) {
    if (!address) return null;
    var lookup = buildNameLookup();
    var candidates = [address.state, address['ISO3166-2-lvl4'], address.territory];
    for (var i = 0; i < candidates.length; i++) {
      var value = candidates[i];
      if (!value) continue;
      var trimmed = String(value).replace(/^US-/i, '').toLowerCase();
      if (lookup[trimmed]) return lookup[trimmed];
    }
    return null;
  }

  /* Fall back to the nearest state centroid when the geocoder names no state.
   *
   * Returns null past MAX_FALLBACK_MILES. Centroids are crude -- El Paso is some
   * 500 miles from the middle of Texas -- but Puerto Rico is 1,600 miles from
   * the nearest one, so the cutoff separates "vague geocode inside a real state"
   * from "territory we have no data for". */
  var MAX_FALLBACK_MILES = 600;

  function nearestStateCode(lat, lon) {
    var best = null;
    var bestDistance = Infinity;
    (metaCache.states || []).forEach(function (state) {
      if (state.lat == null) return;
      var distance = haversine(lat, lon, state.lat, state.lon);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = state.code;
      }
    });
    return bestDistance <= MAX_FALLBACK_MILES ? best : null;
  }

  function nominatim(path, params) {
    var query = Object.keys(params).map(function (key) {
      return encodeURIComponent(key) + '=' + encodeURIComponent(params[key]);
    }).join('&');
    return fetch(NOMINATIM + path + '?' + query, { headers: { Accept: 'application/json' } })
      .catch(function () {
        // A rejected fetch means the network stopped us before any response
        // arrived, so there is no status code to report.
        throw new Error('Could not reach the geocoding service. Check your connection, ' +
                        'or browse by state below.');
      })
      .then(function (response) {
        if (response.status === 429) {
          throw new Error('The geocoding service is rate-limiting us. Wait a moment and try again.');
        }
        if (!response.ok) {
          throw new Error('The geocoding service returned an error (' + response.status + ').');
        }
        return response.json();
      });
  }

  function geocode(query) {
    return nominatim('/search', {
      q: query, format: 'jsonv2', addressdetails: 1, countrycodes: 'us', limit: 1
    }).then(function (results) {
      if (!results || !results.length) return null;
      var hit = results[0];
      return {
        lat: parseFloat(hit.lat), lon: parseFloat(hit.lon),
        label: hit.display_name, address: hit.address
      };
    });
  }

  function reverseGeocode(lat, lon) {
    return nominatim('/reverse', {
      lat: lat, lon: lon, format: 'jsonv2', addressdetails: 1, zoom: 10
    }).then(function (hit) {
      if (!hit || hit.error) return null;
      return { lat: lat, lon: lon, label: hit.display_name, address: hit.address };
    });
  }

  function tileConfig() { return tiles; }

  global.ChurchData = {
    init: init,
    tileConfig: tileConfig,
    mode: currentMode,
    hasAccounts: hasAccounts,
    meta: meta,
    prepare: prepare,
    expandRegion: expandRegion,
    search: search,
    getChurch: getChurch,
    haversine: haversine,
    geocode: geocode,
    reverseGeocode: reverseGeocode,
    stateCodeFromAddress: stateCodeFromAddress,
    nearestStateCode: nearestStateCode
  };
})(window);
