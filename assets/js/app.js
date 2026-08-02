/* ChurchFind UI: search, filtering, list rendering and the Leaflet map.
 *
 * Records come from OpenStreetMap, which is to say from the public. Everything
 * rendered here is built with DOM APIs and textContent rather than innerHTML,
 * and links are scheme-checked before they reach an href.
 */
(function () {
  'use strict';

  var PAGE_SIZE = 50;
  var MAX_MARKERS = 400;   // past this the map turns to soup and Leaflet crawls

  var state = {
    index: null,
    mode: null,          // 'near' when we have an origin point, 'state' when browsing
    origin: null,        // { lat, lon, label, code }
    stateCode: null,
    pool: [],            // churches loaded for the current region
    visible: [],         // pool after filters and sorting
    shown: PAGE_SIZE,
    filters: {
      radius: 25,
      name: '',
      families: new Set(),
      website: false,
      phone: false,
      services: false,
      wheelchair: false
    },
    sort: 'distance'
  };

  // Bumped on every search so a slow neighbour load from an abandoned search
  // cannot fold its results into the current one.
  var searchToken = 0;

  var map = null;
  var mapUnavailable = false;
  var markerLayer = null;
  var markersById = {};

  var dom = {};

  /* ---------------------------------------------------------------- helpers */

  function $(id) { return document.getElementById(id); }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === 'class') node.className = attrs[key];
        else if (key === 'text') node.textContent = attrs[key];
        else if (key === 'html') node.innerHTML = attrs[key];
        else if (attrs[key] !== null && attrs[key] !== undefined && attrs[key] !== false) {
          node.setAttribute(key, attrs[key]);
        }
      });
    }
    (children || []).forEach(function (child) {
      if (child) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  /* Only http(s) links get rendered; anything else came in malformed. */
  function safeUrl(url) {
    if (!url) return null;
    try {
      var parsed = new URL(url, window.location.href);
      return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : null;
    } catch (error) {
      return null;
    }
  }

  function telHref(phone) {
    var digits = String(phone).replace(/[^\d+]/g, '');
    return digits.length >= 7 ? 'tel:' + digits : null;
  }

  function formatNumber(value) {
    return Number(value).toLocaleString('en-US');
  }

  function formatDistance(miles) {
    if (miles < 0.1) return '< 0.1 mi';
    if (miles < 10) return miles.toFixed(1) + ' mi';
    return Math.round(miles) + ' mi';
  }

  function setStatus(message, tone) {
    dom.status.textContent = message || '';
    dom.status.className = 'search-status' + (tone ? ' is-' + tone : '');
  }

  var toastTimer = null;
  function toast(message) {
    dom.toast.textContent = message;
    dom.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { dom.toast.hidden = true; }, 5000);
  }

  /* ------------------------------------------------------------------- boot */

  function init() {
    dom = {
      form: $('search-form'),
      input: $('location-input'),
      geolocate: $('geolocate-btn'),
      status: $('search-status'),
      layout: $('results-layout'),
      list: $('church-list'),
      count: $('results-count'),
      loadMore: $('load-more'),
      sort: $('sort-select'),
      radius: $('radius-input'),
      radiusOut: $('radius-output'),
      nameFilter: $('name-filter'),
      chips: $('family-chips'),
      reset: $('reset-filters'),
      website: $('filter-website'),
      phone: $('filter-phone'),
      services: $('filter-services'),
      wheelchair: $('filter-wheelchair'),
      stateGrid: $('state-grid'),
      aboutStats: $('about-stats'),
      heroCount: $('hero-count'),
      heroStates: $('hero-states'),
      colophon: $('colophon-meta'),
      toast: $('toast')
    };

    bindEvents();

    ChurchData.loadIndex().then(function (index) {
      state.index = index;
      renderIndexBits(index);
      restoreFromUrl();
    }).catch(function (error) {
      setStatus(error.message + ' Run the scraper to generate data/ first.', 'error');
    });
  }

  function bindEvents() {
    dom.form.addEventListener('submit', function (event) {
      event.preventDefault();
      searchByQuery(dom.input.value.trim());
    });

    dom.geolocate.addEventListener('click', searchByGeolocation);

    dom.radius.addEventListener('input', function () {
      state.filters.radius = Number(dom.radius.value);
      dom.radiusOut.textContent = dom.radius.value;
      refresh();
    });

    var nameTimer = null;
    dom.nameFilter.addEventListener('input', function () {
      clearTimeout(nameTimer);
      nameTimer = setTimeout(function () {
        state.filters.name = dom.nameFilter.value.trim().toLowerCase();
        refresh();
      }, 180);
    });

    ['website', 'phone', 'services', 'wheelchair'].forEach(function (key) {
      dom[key].addEventListener('change', function () {
        state.filters[key] = dom[key].checked;
        refresh();
      });
    });

    dom.sort.addEventListener('change', function () {
      state.sort = dom.sort.value;
      refresh();
    });

    dom.loadMore.addEventListener('click', function () {
      state.shown += PAGE_SIZE;
      renderList();
    });

    dom.reset.addEventListener('click', resetFilters);
  }

  function renderIndexBits(index) {
    var total = index.totals.churches;
    dom.heroCount.textContent = formatNumber(total);

    // Say what was actually scraped rather than a hardcoded "all 50 states" --
    // a partial run should not be advertised as complete coverage.
    var stateCount = index.states.length;
    var hasDC = index.states.some(function (s) { return s.code === 'DC'; });
    dom.heroStates.textContent = (stateCount === 51 && hasDC)
      ? 'all 50 states and DC'
      : formatNumber(hasDC ? stateCount - 1 : stateCount) +
        (hasDC ? ' states and DC' : (stateCount === 1 ? ' state' : ' states'));

    dom.chips.textContent = '';
    Object.keys(index.families).forEach(function (family) {
      var info = index.families[family];
      var chip = el('button', {
        type: 'button',
        class: 'chip',
        'data-family': family,
        'aria-pressed': 'false'
      }, [
        el('span', { text: info.label }),
        el('span', { class: 'chip-count', text: formatNumber(info.count) })
      ]);
      chip.addEventListener('click', function () { toggleFamily(family, chip); });
      dom.chips.appendChild(chip);
    });

    dom.stateGrid.textContent = '';
    index.states.forEach(function (entry) {
      var link = el('button', { type: 'button', class: 'state-card' }, [
        el('span', { class: 'state-name', text: entry.name }),
        el('span', { class: 'state-count', text: formatNumber(entry.count) })
      ]);
      link.addEventListener('click', function () { browseState(entry); });
      dom.stateGrid.appendChild(el('li', null, [link]));
    });

    dom.aboutStats.textContent = '';
    [
      ['Churches indexed', formatNumber(total)],
      ['States covered', formatNumber(index.states.length)],
      ['With a street address', formatNumber(index.totals.with_address)],
      ['With a website', formatNumber(index.totals.with_website)],
      ['With a phone number', formatNumber(index.totals.with_phone)]
    ].forEach(function (pair) {
      dom.aboutStats.appendChild(el('dt', { text: pair[0] }));
      dom.aboutStats.appendChild(el('dd', { text: pair[1] }));
    });

    var snapshot = index.osm_snapshot ? index.osm_snapshot.slice(0, 10) : 'unknown';
    dom.colophon.textContent = 'Snapshot of OpenStreetMap taken ' + snapshot +
      ' · ' + formatNumber(total) + ' churches · scraped ' + index.generated_at.slice(0, 10) + '.';
  }

  /* --------------------------------------------------------------- searching */

  function searchByQuery(query) {
    if (!query) {
      setStatus('Type a town, ZIP code or address to search.', 'error');
      dom.input.focus();
      return;
    }
    setStatus('Looking up “' + query + '”…');
    ChurchData.geocode(query).then(function (place) {
      if (!place) {
        setStatus('No US location matched “' + query + '”. Try a ZIP code or "City, State".', 'error');
        return;
      }
      var code = ChurchData.stateCodeFromAddress(place.address, state.index) ||
                 ChurchData.nearestStateCode(place.lat, place.lon, state.index);
      loadAndShow(place, code, query);
    }).catch(function (error) {
      setStatus(error.message, 'error');
    });
  }

  function searchByGeolocation() {
    if (!navigator.geolocation) {
      setStatus('This browser does not offer location access. Type a place instead.', 'error');
      return;
    }
    setStatus('Asking your browser for your location…');
    navigator.geolocation.getCurrentPosition(function (position) {
      var lat = position.coords.latitude;
      var lon = position.coords.longitude;
      setStatus('Found you. Working out which state that is…');
      ChurchData.reverseGeocode(lat, lon).then(function (place) {
        var resolved = place || { lat: lat, lon: lon, label: 'your location', address: null };
        resolved.lat = lat;
        resolved.lon = lon;
        var code = ChurchData.stateCodeFromAddress(resolved.address, state.index) ||
                   ChurchData.nearestStateCode(lat, lon, state.index);
        loadAndShow(resolved, code, '');
      }).catch(function () {
        var code = ChurchData.nearestStateCode(lat, lon, state.index);
        loadAndShow({ lat: lat, lon: lon, label: 'your location' }, code, '');
      });
    }, function (error) {
      var reasons = {
        1: 'Location access was denied. Type a place instead.',
        2: 'Your location is unavailable right now. Type a place instead.',
        3: 'Locating you timed out. Type a place instead.'
      };
      setStatus(reasons[error.code] || 'Could not get your location.', 'error');
    }, { timeout: 10000, maximumAge: 300000 });
  }

  function loadAndShow(place, code, query) {
    if (!code) {
      setStatus('That location is outside the 50 states and DC, which is all this ' +
                'dataset covers. US territories are not included.', 'error');
      return;
    }
    var stateName = stateNameFor(code);
    setStatus('Loading churches in ' + stateName + '…');

    var token = ++searchToken;

    ChurchData.loadState(code).then(function (churches) {
      if (token !== searchToken) return;   // a newer search overtook this one

      state.mode = 'near';
      state.origin = { lat: place.lat, lon: place.lon, label: place.label || query, code: code };
      state.stateCode = code;
      state.pool = churches;
      state.sort = 'distance';
      dom.sort.value = 'distance';
      dom.radius.disabled = false;
      dom.radius.closest('.filter-block').hidden = false;

      var shortLabel = shortenPlace(place.label || query);
      setStatus('Showing churches near ' + shortLabel + '.', 'ok');
      updateUrl({ q: query || (place.lat.toFixed(4) + ',' + place.lon.toFixed(4)) });
      reveal();
      refresh(true);

      // Then quietly widen the net. Somebody in Kansas City needs both sides of
      // the state line, but they should not wait on eight files to see the
      // first result.
      ChurchData.loadNeighbors(code).then(function (extra) {
        if (token !== searchToken || !extra.length) return;
        state.pool = state.pool.concat(extra);
        refresh();
      });
    }).catch(function (error) {
      if (token === searchToken) setStatus(error.message, 'error');
    });
  }

  function browseState(entry) {
    setStatus('Loading every church in ' + entry.name + '…');
    var token = ++searchToken;

    ChurchData.loadState(entry.code).then(function (churches) {
      if (token !== searchToken) return;
      state.mode = 'state';
      state.origin = { lat: entry.lat, lon: entry.lon, label: entry.name, code: entry.code };
      state.stateCode = entry.code;
      state.pool = churches;
      state.sort = 'name';
      dom.sort.value = 'name';
      // Radius is meaningless when browsing a whole state, so hide it.
      dom.radius.closest('.filter-block').hidden = true;

      setStatus('Showing all ' + formatNumber(churches.length) + ' churches in ' + entry.name + '.', 'ok');
      updateUrl({ state: entry.code });
      reveal();
      refresh(true);
    }).catch(function (error) {
      if (token === searchToken) setStatus(error.message, 'error');
    });
  }

  function stateNameFor(code) {
    var match = (state.index.states || []).filter(function (s) { return s.code === code; })[0];
    return match ? match.name : code;
  }

  /* Nominatim returns the full postal hierarchy; the first two parts are plenty. */
  function shortenPlace(label) {
    if (!label) return 'your location';
    return label.split(',').slice(0, 2).join(',').trim();
  }

  function reveal() {
    if (dom.layout.hidden) {
      dom.layout.hidden = false;
      ensureMap();
      // Leaflet measures its container on creation. The layout has only just
      // been un-hidden, so give it a frame and re-measure.
      if (map) requestAnimationFrame(function () { map.invalidateSize(); });
    }
    dom.layout.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* --------------------------------------------------------------- filtering */

  function toggleFamily(family, chip) {
    if (state.filters.families.has(family)) state.filters.families.delete(family);
    else state.filters.families.add(family);
    chip.setAttribute('aria-pressed', state.filters.families.has(family) ? 'true' : 'false');
    chip.classList.toggle('is-on', state.filters.families.has(family));
    refresh();
  }

  function resetFilters() {
    state.filters = {
      radius: 25, name: '', families: new Set(),
      website: false, phone: false, services: false, wheelchair: false
    };
    dom.radius.value = 25;
    dom.radiusOut.textContent = '25';
    dom.nameFilter.value = '';
    ['website', 'phone', 'services', 'wheelchair'].forEach(function (key) { dom[key].checked = false; });
    Array.prototype.forEach.call(dom.chips.children, function (chip) {
      chip.setAttribute('aria-pressed', 'false');
      chip.classList.remove('is-on');
    });
    refresh();
  }

  function applyFilters() {
    var filters = state.filters;
    var origin = state.origin;
    var nearMode = state.mode === 'near';

    var results = [];
    for (var i = 0; i < state.pool.length; i++) {
      var church = state.pool[i];

      if (filters.families.size && !filters.families.has(church.family)) continue;
      if (filters.name && church.name.toLowerCase().indexOf(filters.name) === -1) continue;
      if (filters.website && !church.website) continue;
      if (filters.phone && !church.phone) continue;
      if (filters.services && !church.services) continue;
      if (filters.wheelchair && church.wheelchair !== 'yes') continue;

      var distance = origin ? ChurchData.haversine(origin.lat, origin.lon, church.lat, church.lon) : 0;
      if (nearMode && distance > filters.radius) continue;

      results.push({ church: church, distance: distance });
    }

    results.sort(function (a, b) {
      if (state.sort === 'name') return a.church.name.localeCompare(b.church.name);
      if (state.sort === 'denomination') {
        var left = a.church.denomination || 'zzz';
        var right = b.church.denomination || 'zzz';
        return left.localeCompare(right) || a.distance - b.distance;
      }
      return a.distance - b.distance;
    });

    return results;
  }

  function refresh(resetPaging) {
    if (!state.pool.length && state.mode === null) return;
    state.visible = applyFilters();
    if (resetPaging) state.shown = PAGE_SIZE;
    renderCount();
    renderList();
    renderMap();
  }

  function renderCount() {
    var total = state.visible.length;
    if (!total) {
      dom.count.textContent = 'No churches match these filters';
      return;
    }
    var suffix = state.mode === 'near'
      ? ' within ' + state.filters.radius + ' miles'
      : ' in ' + stateNameFor(state.stateCode);
    dom.count.textContent = formatNumber(total) + (total === 1 ? ' church' : ' churches') + suffix;
  }

  /* ----------------------------------------------------------------- results */

  function badge(church) {
    var label = church.denomination || 'Denomination not listed';
    return el('span', {
      class: 'badge fam-' + (church.denomination ? church.family : 'unknown'),
      text: label
    });
  }

  function addressLine(church) {
    var parts = [church.address, church.city, church.state].filter(Boolean);
    if (church.postcode) parts.push(church.postcode);
    return parts.join(', ');
  }

  function directionsUrl(church) {
    return 'https://www.openstreetmap.org/directions?to=' + church.lat + '%2C' + church.lon;
  }

  function buildCard(entry) {
    var church = entry.church;
    var actions = [];

    var site = safeUrl(church.website);
    if (site) actions.push(el('a', { class: 'action', href: site, rel: 'noopener nofollow', target: '_blank', text: 'Website' }));

    var tel = telHref(church.phone);
    if (tel) actions.push(el('a', { class: 'action', href: tel, text: church.phone }));

    actions.push(el('a', {
      class: 'action', href: directionsUrl(church), rel: 'noopener', target: '_blank', text: 'Directions'
    }));

    // Clicking anywhere on the card also does this, but a mouse-only affordance
    // is no affordance at all -- keyboard and screen-reader users need a real
    // control, and it cannot be the card itself because the card holds links.
    if (!mapUnavailable) {
      var locate = el('button', {
        type: 'button', class: 'action',
        'aria-label': 'Show ' + church.name + ' on the map',
        text: 'Show on map'
      });
      locate.addEventListener('click', function () { focusChurch(church.id); });
      actions.push(locate);
    }

    var meta = [];
    if (church.services) meta.push(el('p', { class: 'services', text: 'Services: ' + church.services }));
    else if (church.hours) meta.push(el('p', { class: 'services', text: 'Open: ' + church.hours }));
    if (church.wheelchair === 'yes') meta.push(el('p', { class: 'access', text: '♿ Wheelchair accessible' }));

    var address = addressLine(church);

    var card = el('li', { class: 'church-card', 'data-id': church.id }, [
      el('div', { class: 'card-head' }, [
        el('h3', { class: 'church-name', text: church.name }),
        state.mode === 'near'
          ? el('span', { class: 'distance', text: formatDistance(entry.distance) })
          : el('span', { class: 'distance muted', text: church.city || '' })
      ]),
      badge(church),
      address ? el('p', { class: 'address', text: address }) : el('p', { class: 'address muted', text: 'Address not recorded' })
    ].concat(meta).concat([
      el('div', { class: 'actions' }, actions)
    ]));

    // Mouse convenience on top of the button above: click the card anywhere.
    card.addEventListener('click', function (event) {
      if (event.target.closest('a, button')) return;   // let real controls handle themselves
      focusChurch(church.id);
    });

    return card;
  }

  function renderList() {
    dom.list.textContent = '';
    var slice = state.visible.slice(0, state.shown);
    var fragment = document.createDocumentFragment();
    slice.forEach(function (entry) {
      fragment.appendChild(buildCard(entry));
    });
    dom.list.appendChild(fragment);

    var remaining = state.visible.length - slice.length;
    dom.loadMore.hidden = remaining <= 0;
    if (remaining > 0) {
      dom.loadMore.textContent = 'Show ' + formatNumber(Math.min(PAGE_SIZE, remaining)) +
        ' more (' + formatNumber(remaining) + ' left)';
    }
  }

  /* --------------------------------------------------------------------- map */

  /* The map is a bonus, not the product. If Leaflet or the tile host is
     unreachable the list still has to work, so failures here are swallowed and
     the pane is removed rather than allowed to break the search. */
  function ensureMap() {
    if (map || mapUnavailable) return;
    if (typeof L === 'undefined') {
      disableMap();
      return;
    }
    try {
      map = L.map('map', { scrollWheelZoom: false });
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      }).addTo(map);
      markerLayer = L.layerGroup().addTo(map);
      map.setView([39.5, -98.35], 4);
    } catch (error) {
      disableMap();
    }
  }

  function disableMap() {
    mapUnavailable = true;
    map = null;
    var pane = document.querySelector('.map-pane');
    if (pane) pane.hidden = true;
    document.querySelector('.layout').classList.add('no-map');
  }

  function renderMap() {
    if (!map) return;
    markerLayer.clearLayers();
    markersById = {};

    var entries = state.visible.slice(0, MAX_MARKERS);
    var bounds = [];

    if (state.origin && state.mode === 'near') {
      var originMarker = L.circleMarker([state.origin.lat, state.origin.lon], {
        radius: 8, color: '#b4791f', weight: 3, fillColor: '#f0c46a', fillOpacity: 0.9
      }).bindPopup('Your search location');
      originMarker.addTo(markerLayer);
      bounds.push([state.origin.lat, state.origin.lon]);
    }

    entries.forEach(function (entry) {
      var church = entry.church;
      var marker = L.circleMarker([church.lat, church.lon], {
        radius: 6, color: '#2f3f6b', weight: 2, fillColor: '#5b7ac9', fillOpacity: 0.85
      });
      marker.bindPopup(popupFor(entry));
      marker.addTo(markerLayer);
      markersById[church.id] = marker;
      bounds.push([church.lat, church.lon]);
    });

    if (bounds.length > 1) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 14 });
    else if (bounds.length === 1) map.setView(bounds[0], 12);

    if (state.visible.length > MAX_MARKERS) {
      toast('Map shows the closest ' + formatNumber(MAX_MARKERS) + ' of ' +
            formatNumber(state.visible.length) + ' matches. Narrow the filters to see the rest.');
    }
  }

  /* Popups take a DOM node, so nothing here needs HTML escaping. */
  function popupFor(entry) {
    var church = entry.church;
    var children = [el('strong', { text: church.name })];
    if (church.denomination) children.push(el('div', { class: 'popup-den', text: church.denomination }));
    var address = addressLine(church);
    if (address) children.push(el('div', { class: 'popup-addr', text: address }));
    if (state.mode === 'near') {
      children.push(el('div', { class: 'popup-dist', text: formatDistance(entry.distance) + ' away' }));
    }
    var site = safeUrl(church.website);
    if (site) children.push(el('a', { href: site, target: '_blank', rel: 'noopener nofollow', text: 'Visit website' }));
    return el('div', { class: 'popup' }, children);
  }

  function focusChurch(id) {
    if (mapUnavailable) return;
    var marker = markersById[id];
    if (!marker) {
      toast('That church is outside the range shown on the map.');
      return;
    }
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 14));
    marker.openPopup();
    document.querySelectorAll('.church-card.is-active').forEach(function (node) {
      node.classList.remove('is-active');
    });
    var card = dom.list.querySelector('[data-id="' + CSS.escape(id) + '"]');
    if (card) card.classList.add('is-active');
  }

  /* ------------------------------------------------------------ url plumbing */

  function updateUrl(params) {
    var search = new URLSearchParams();
    Object.keys(params).forEach(function (key) {
      if (params[key]) search.set(key, params[key]);
    });
    var url = window.location.pathname + (search.toString() ? '?' + search : '');
    window.history.replaceState(null, '', url);
  }

  function restoreFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var stateParam = params.get('state');
    var query = params.get('q');

    if (stateParam) {
      var entry = (state.index.states || []).filter(function (s) {
        return s.code === stateParam.toUpperCase();
      })[0];
      if (entry) { browseState(entry); return; }
    }
    if (query) {
      dom.input.value = query;
      searchByQuery(query);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
