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

  function expand(payload) {
    var fields = payload.fields;
    return payload.churches.map(function (row) {
      var church = {};
      for (var i = 0; i < fields.length; i++) church[fields[i]] = row[i];
      return church;
    });
  }

  function loadState(code) {
    code = String(code).toUpperCase();
    if (!stateCache[code]) {
      stateCache[code] = fetch(STATE_URL + code + '.json')
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
    ['hasWebsite', 'hasPhone', 'hasServices', 'wheelchair'].forEach(function (key) {
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

  global.ChurchData = {
    init: init,
    mode: currentMode,
    hasAccounts: hasAccounts,
    meta: meta,
    prepare: prepare,
    expandRegion: expandRegion,
    search: search,
    haversine: haversine,
    geocode: geocode,
    reverseGeocode: reverseGeocode,
    stateCodeFromAddress: stateCodeFromAddress,
    nearestStateCode: nearestStateCode
  };
})(window);
