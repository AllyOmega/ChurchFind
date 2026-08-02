/* Data access for ChurchFind: loading state files, geocoding, distance math.
 *
 * State files are fetched on demand and kept in memory for the session. A search
 * near a border also pulls in the neighbouring states, otherwise someone
 * standing in Kansas City would only ever see half the city.
 */
(function (global) {
  'use strict';

  var INDEX_URL = 'data/index.json';
  var STATE_URL = 'data/states/';
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

  var indexPromise = null;
  var stateCache = {};
  var nameToCode = null;

  function loadIndex() {
    if (!indexPromise) {
      indexPromise = fetch(INDEX_URL).then(function (response) {
        if (!response.ok) throw new Error('Could not load the church index (' + response.status + ').');
        return response.json();
      });
    }
    return indexPromise;
  }

  /* Expand the compact row format back into objects, once per state. */
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

  /* Load a state plus its neighbours so border searches aren't cut in half. */
  function loadRegion(code) {
    code = String(code).toUpperCase();
    var codes = [code].concat(ADJACENT[code] || []);
    return Promise.all(codes.map(function (c) {
      return loadState(c).catch(function () { return []; });   // a missing neighbour shouldn't fail the search
    })).then(function (lists) {
      return Array.prototype.concat.apply([], lists);
    });
  }

  function haversine(lat1, lon1, lat2, lon2) {
    var toRad = Math.PI / 180;
    var dLat = (lat2 - lat1) * toRad;
    var dLon = (lon2 - lon1) * toRad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return EARTH_RADIUS_MILES * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function buildNameLookup(index) {
    if (nameToCode) return nameToCode;
    nameToCode = {};
    index.states.forEach(function (state) {
      nameToCode[state.name.toLowerCase()] = state.code;
      nameToCode[state.code.toLowerCase()] = state.code;
    });
    return nameToCode;
  }

  function stateCodeFromAddress(address, index) {
    if (!address) return null;
    var lookup = buildNameLookup(index);
    var candidates = [address.state, address['ISO3166-2-lvl4'], address.territory];
    for (var i = 0; i < candidates.length; i++) {
      var value = candidates[i];
      if (!value) continue;
      var trimmed = String(value).replace(/^US-/i, '').toLowerCase();
      if (lookup[trimmed]) return lookup[trimmed];
    }
    return null;
  }

  /* Fall back to the nearest state centroid when the geocoder gives us no state.
   * Crude, but it only fires for results that landed outside a named state. */
  function nearestStateCode(lat, lon, index) {
    var best = null;
    var bestDistance = Infinity;
    index.states.forEach(function (state) {
      var distance = haversine(lat, lon, state.lat, state.lon);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = state.code;
      }
    });
    return best;
  }

  function nominatim(path, params) {
    var query = Object.keys(params).map(function (key) {
      return encodeURIComponent(key) + '=' + encodeURIComponent(params[key]);
    }).join('&');
    return fetch(NOMINATIM + path + '?' + query, { headers: { Accept: 'application/json' } })
      .catch(function () {
        // A rejected fetch means the network or a blocker stopped us before any
        // response arrived, so there is no status code to report.
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
      q: query,
      format: 'jsonv2',
      addressdetails: 1,
      countrycodes: 'us',
      limit: 1
    }).then(function (results) {
      if (!results || !results.length) return null;
      var hit = results[0];
      return {
        lat: parseFloat(hit.lat),
        lon: parseFloat(hit.lon),
        label: hit.display_name,
        address: hit.address
      };
    });
  }

  function reverseGeocode(lat, lon) {
    return nominatim('/reverse', {
      lat: lat,
      lon: lon,
      format: 'jsonv2',
      addressdetails: 1,
      zoom: 10
    }).then(function (hit) {
      if (!hit || hit.error) return null;
      return { lat: lat, lon: lon, label: hit.display_name, address: hit.address };
    });
  }

  global.ChurchData = {
    loadIndex: loadIndex,
    loadState: loadState,
    loadRegion: loadRegion,
    haversine: haversine,
    geocode: geocode,
    reverseGeocode: reverseGeocode,
    stateCodeFromAddress: stateCodeFromAddress,
    nearestStateCode: nearestStateCode
  };
})(window);
