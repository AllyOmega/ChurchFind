/* ChurchFind UI: search, filtering, list rendering and the Leaflet map.
 *
 * Records come from OpenStreetMap, which is to say from the public. Everything
 * rendered here is built with DOM APIs and textContent rather than innerHTML,
 * and links are scheme-checked before they reach an href.
 *
 * Search goes through ChurchData, which talks to the API when a server is
 * running and to per-state JSON files when it is not. This file does not care
 * which; it only waits on promises.
 */
(function () {
  'use strict';

  var PAGE_SIZE = 50;
  var MAX_MARKERS = 400;   // past this the map turns to soup and Leaflet crawls

  var state = {
    meta: null,
    mode: null,          // 'near' when we have an origin point, 'state' when browsing
    origin: null,        // { lat, lon, label, code }
    stateCode: null,
    results: [],         // everything rendered so far, grown by "show more"
    total: 0,
    filters: {
      radius: 25,
      name: '',
      families: [],
      denominations: [],
      hasWebsite: false,
      hasPhone: false,
      hasServices: false,
      wheelchair: false,
      hearingLoop: false,
      serviceDays: [],
      servicePeriods: [],
      churchmanship: []
    },
    sort: 'distance'
  };

  // Bumped on every search so a slow response from an abandoned search cannot
  // overwrite the current results.
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

  function formatNumber(value) { return Number(value).toLocaleString('en-US'); }

  function formatDistance(miles) {
    if (miles == null) return '';
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
      form: $('search-form'), input: $('location-input'), geolocate: $('geolocate-btn'),
      status: $('search-status'), layout: $('results-layout'), list: $('church-list'),
      hero: $('hero'),
      detail: $('church-detail'), detailBody: $('detail-body'), detailBack: $('detail-back'),
      results: document.querySelector('.results'), mapPane: document.querySelector('.map-pane'),
      count: $('results-count'), loadMore: $('load-more'), sort: $('sort-select'),
      radius: $('radius-input'), radiusOut: $('radius-output'), nameFilter: $('name-filter'),
      chips: $('family-chips'), denomBlock: $('denomination-block'),
      denomSelect: $('denomination-select'), denomChosen: $('denomination-chosen'),
      reset: $('reset-filters'),
      filtersToggle: $('filters-toggle'), filtersBody: $('filters-body'),
      filtersToggleText: $('filters-toggle-text'),
      website: $('filter-website'), phone: $('filter-phone'),
      services: $('filter-services'), wheelchair: $('filter-wheelchair'),
      hearing: $('filter-hearing'), serviceBlock: $('service-block'),
      cmBlock: $('churchmanship-block'), cmChips: $('churchmanship-chips'),
      cmNote: $('churchmanship-note'),
      serviceDays: $('service-day-chips'), servicePeriods: $('service-period-chips'),
      serviceNote: $('service-note'),
      stateGrid: $('state-grid'), aboutStats: $('about-stats'),
      heroCount: $('hero-count'), heroStates: $('hero-states'),
      colophon: $('colophon-meta'), toast: $('toast'),
      account: $('account-area'), savedLink: $('saved-link'),
      queueLink: $('queue-link'), modeNote: $('mode-note')
    };

    bindEvents();

    ChurchData.init().then(function (boot) {
      state.meta = boot.meta;
      renderMeta(boot.meta);
      renderModeNote(boot.mode);
      return ChurchAccount.init(ChurchData.hasAccounts());
    }).then(function () {
      ChurchAccount.onChange(renderAccount);
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

    // The slider fires continuously; searching on every pixel would hammer the
    // API, so the label updates live and the search waits for a pause.
    var radiusTimer = null;
    dom.radius.addEventListener('input', function () {
      state.filters.radius = Number(dom.radius.value);
      dom.radiusOut.textContent = dom.radius.value;
      clearTimeout(radiusTimer);
      radiusTimer = setTimeout(rerun, 220);
    });

    var nameTimer = null;
    dom.nameFilter.addEventListener('input', function () {
      clearTimeout(nameTimer);
      nameTimer = setTimeout(function () {
        state.filters.name = dom.nameFilter.value.trim();
        rerun();
      }, 250);
    });

    ['website', 'phone', 'services', 'wheelchair', 'hearing'].forEach(function (key) {
      var filterKey = key === 'wheelchair' ? 'wheelchair'
        : key === 'hearing' ? 'hearingLoop'
        : 'has' + key.charAt(0).toUpperCase() + key.slice(1);
      dom[key].addEventListener('change', function () {
        state.filters[filterKey] = dom[key].checked;
        rerun();
      });
    });

    dom.sort.addEventListener('change', function () {
      state.sort = dom.sort.value;
      rerun();
    });

    dom.loadMore.addEventListener('click', loadMore);
    dom.reset.addEventListener('click', resetFilters);
    dom.detailBack.addEventListener('click', function () { closeDetail(); });
    setUpFilterCollapse();

    // Back and Forward move between the results and a church, because both are
    // real URLs. `push: false` stops the handler pushing the state it is
    // reacting to and stacking duplicates.
    window.addEventListener('popstate', function (event) {
      var id = (event.state && event.state.church) ||
               new URLSearchParams(window.location.search).get('church');
      if (!id) { closeDetail('none'); return; }
      var code = (event.state && event.state.state) ||
                 new URLSearchParams(window.location.search).get('state');
      ChurchData.getChurch(id, code).then(function (church) {
        if (church) openChurch(church, 'none'); else closeDetail('none');
      }).catch(function () { closeDetail('none'); });
    });
    dom.denomSelect.addEventListener('change', function () {
      addDenomination(dom.denomSelect.value);
      dom.denomSelect.value = '';
    });
    dom.savedLink.addEventListener('click', function (event) {
      event.preventDefault();
      showSaved();
    });
    dom.queueLink.addEventListener('click', function (event) {
      event.preventDefault();
      ChurchReviews.openQueue();
    });
  }

  /* ------------------------------------------------------------- chrome bits */

  function renderModeNote(mode) {
    if (mode === 'api') {
      dom.modeNote.textContent = '';
      dom.modeNote.hidden = true;
      return;
    }
    dom.modeNote.textContent =
      'Running without the API server, so searches use the bundled data files and ' +
      'accounts are unavailable. See the README to start the server.';
    dom.modeNote.hidden = false;
    dom.savedLink.hidden = true;
  }

  function renderMeta(meta) {
    dom.heroCount.textContent = formatNumber(meta.total);

    // Say what was actually loaded rather than a hardcoded "all 50 states".
    var codes = (meta.states || []).map(function (s) { return s.code; });
    var hasDC = codes.indexOf('DC') !== -1;
    var stateCount = codes.length - (hasDC ? 1 : 0);
    dom.heroStates.textContent = (stateCount === 50 && hasDC)
      ? 'all 50 states and DC'
      : formatNumber(stateCount) + (stateCount === 1 ? ' state' : ' states') +
        (hasDC ? ' and DC' : '');

    renderServiceFilters(meta);

    dom.chips.textContent = '';
    meta.families.forEach(function (info) {
      var chip = el('button', {
        type: 'button', class: 'chip', 'data-family': info.key, 'aria-pressed': 'false'
      }, [
        el('span', { text: info.label }),
        el('span', { class: 'chip-count', text: formatNumber(info.count) })
      ]);
      chip.addEventListener('click', function () { toggleFamily(info.key, chip); });
      dom.chips.appendChild(chip);
    });

    dom.stateGrid.textContent = '';
    (meta.states || []).forEach(function (entry) {
      var link = el('button', { type: 'button', class: 'state-card' }, [
        el('span', { class: 'state-name', text: entry.name }),
        el('span', { class: 'state-count', text: formatNumber(entry.count) })
      ]);
      link.addEventListener('click', function () { browseState(entry); });
      dom.stateGrid.appendChild(el('li', null, [link]));
    });

    var totals = meta.totals || {};
    dom.aboutStats.textContent = '';
    var rows = [['Churches indexed', formatNumber(meta.total)],
                ['States covered', formatNumber((meta.states || []).length)]];
    if (totals.with_address != null) {
      rows.push(['With a street address', formatNumber(totals.with_address)],
                ['With a website', formatNumber(totals.with_website)],
                ['With a phone number', formatNumber(totals.with_phone)]);
    }
    rows.push(['Distinct denominations',
               formatNumber(Object.keys(meta.denominations || {}).reduce(function (sum, key) {
                 return sum + meta.denominations[key].length;
               }, 0))]);
    rows.forEach(function (pair) {
      dom.aboutStats.appendChild(el('dt', { text: pair[0] }));
      dom.aboutStats.appendChild(el('dd', { text: pair[1] }));
    });

    var dataset = meta.dataset || {};
    var snapshot = (dataset.osm_snapshot || '').slice(0, 10) || 'unknown';
    dom.colophon.textContent = 'Snapshot of OpenStreetMap taken ' + snapshot + ' · ' +
      formatNumber(meta.total) + ' churches · scraped ' +
      (dataset.generated_at || '').slice(0, 10) + '.';
  }

  var WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var PERIOD_FALLBACK = [
    { key: 'early', label: 'Before 8am' }, { key: 'morning', label: 'Morning' },
    { key: 'midday', label: 'Midday' }, { key: 'afternoon', label: 'Afternoon' },
    { key: 'evening', label: 'Evening' }
  ];

  /* Only 3% of churches have a service time recorded, so the panel says so
     rather than letting someone filter to almost nothing and conclude the site
     is broken. */
  /* Churchmanship bands. Offered only when the Anglican family is selected --
     the concept does not apply to a Baptist chapel, and a filter that appears
     everywhere invites people to use it where it means nothing.

     422 of 2,793 Anglican parishes have a reading, so this hides most of them.
     The note says exactly how many rather than letting the count quietly drop:
     a filter that silently discards five sixths of the candidates is worse than
     no filter, because it looks like an answer. */
  var CHURCHMANSHIP_BANDS = [
    { key: 'high',        label: 'High church' },
    { key: 'low',         label: 'Low church' },
    { key: 'catholic',    label: 'Anglo-Catholic' },
    { key: 'evangelical', label: 'Evangelical' }
  ];

  function renderChurchmanshipFilter() {
    var anglicanSelected = state.filters.families.indexOf('anglican') !== -1;
    dom.cmBlock.hidden = !anglicanSelected;
    if (!anglicanSelected) {
      if (state.filters.churchmanship.length) {
        state.filters.churchmanship = [];
        return true;                       // caller must re-run
      }
      return false;
    }

    if (!dom.cmChips.childNodes.length) {
      CHURCHMANSHIP_BANDS.forEach(function (band) {
        var chip = el('button', {
          type: 'button', class: 'chip', 'aria-pressed': 'false', text: band.label
        });
        chip.addEventListener('click', function () {
          var chosen = state.filters.churchmanship;
          var at = chosen.indexOf(band.key);
          if (at === -1) chosen.push(band.key); else chosen.splice(at, 1);
          chip.classList.toggle('is-on');
          chip.setAttribute('aria-pressed', at === -1 ? 'true' : 'false');
          rerun();
        });
        dom.cmChips.appendChild(chip);
      });
    }
    updateChurchmanshipNote();
    return false;
  }

  function updateChurchmanshipNote() {
    if (!dom.cmNote) return;
    dom.cmNote.textContent = state.filters.churchmanship.length
      ? 'Only parishes with a churchmanship reading can match — most have none, ' +
        'and those are hidden while this is on.'
      : 'Readings come from parish websites and Wikipedia. Around one Anglican ' +
        'parish in six has one.';
  }

  function renderServiceFilters(meta) {
    dom.serviceBlock.hidden = false;

    dom.serviceDays.textContent = '';
    WEEKDAY_LABELS.forEach(function (label, index) {
      var chip = el('button', {
        type: 'button', class: 'chip chip-day', 'aria-pressed': 'false',
        'aria-label': label
      }, [el('span', { text: label })]);
      chip.addEventListener('click', function () {
        toggleInList(state.filters.serviceDays, String(index), chip);
      });
      dom.serviceDays.appendChild(chip);
    });

    dom.servicePeriods.textContent = '';
    (meta.servicePeriods || PERIOD_FALLBACK).forEach(function (period) {
      var chip = el('button', {
        type: 'button', class: 'chip', 'aria-pressed': 'false'
      }, [el('span', { text: period.label })]);
      chip.addEventListener('click', function () {
        toggleInList(state.filters.servicePeriods, period.key, chip);
      });
      dom.servicePeriods.appendChild(chip);
    });

    var known = meta.withServiceTimes;
    dom.serviceNote.textContent = known != null
      ? 'Only ' + formatNumber(known) + ' churches have a service time recorded in ' +
        'OpenStreetMap, so these filters search a small slice of the data.'
      : 'Service times come from OpenStreetMap and most churches have none recorded.';
  }

  function toggleInList(list, value, chip) {
    var index = list.indexOf(value);
    if (index === -1) list.push(value);
    else list.splice(index, 1);
    var on = index === -1;
    chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    chip.classList.toggle('is-on', on);
    rerun();
  }

  function renderAccount(user) {
    dom.account.textContent = '';
    if (!ChurchAccount.available()) return;

    if (!user) {
      var signIn = el('button', { type: 'button', class: 'link-btn', text: 'Sign in' });
      signIn.addEventListener('click', function () { ChurchAccount.open('signin'); });
      var signUp = el('button', { type: 'button', class: 'btn btn-small', text: 'Create account' });
      signUp.addEventListener('click', function () { ChurchAccount.open('signup'); });
      dom.account.appendChild(signIn);
      dom.account.appendChild(signUp);
      dom.savedLink.hidden = true;
      dom.queueLink.hidden = true;
      return;
    }

    dom.savedLink.hidden = false;
    dom.queueLink.hidden = !user.isModerator;
    var who = el('span', { class: 'account-who', text: user.displayName || user.email });
    var out = el('button', { type: 'button', class: 'link-btn', text: 'Sign out' });
    out.addEventListener('click', function () {
      ChurchAccount.signOut().then(function () { setStatus('Signed out.', 'ok'); });
    });
    dom.account.appendChild(who);
    dom.account.appendChild(out);

    // Re-render the list so save stars reflect the new user.
    if (state.results.length) renderList();
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
      var code = ChurchData.stateCodeFromAddress(place.address) ||
                 ChurchData.nearestStateCode(place.lat, place.lon);
      startNearSearch(place, code, query);
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
      ChurchData.reverseGeocode(lat, lon).then(function (place) {
        var resolved = place || { lat: lat, lon: lon, label: 'your location', address: null };
        resolved.lat = lat;
        resolved.lon = lon;
        startNearSearch(resolved,
          ChurchData.stateCodeFromAddress(resolved.address) || ChurchData.nearestStateCode(lat, lon),
          '');
      }).catch(function () {
        startNearSearch({ lat: lat, lon: lon, label: 'your location' },
                        ChurchData.nearestStateCode(lat, lon), '');
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

  function startNearSearch(place, code, query) {
    if (!code) {
      setStatus('That location is outside the 50 states and DC, which is all this ' +
                'dataset covers. US territories are not included.', 'error');
      return;
    }

    var token = ++searchToken;
    state.mode = 'near';
    state.origin = { lat: place.lat, lon: place.lon, label: place.label || query, code: code };
    state.stateCode = code;
    state.sort = 'distance';
    dom.sort.value = 'distance';
    dom.radius.closest('.filter-block').hidden = false;

    setStatus('Loading churches near ' + shortenPlace(place.label || query) + '…');

    ChurchData.prepare(code).then(function () {
      if (token !== searchToken) return null;
      return runSearch(token, true).then(function () {
        setStatus('Showing churches near ' + shortenPlace(place.label || query) + '.', 'ok');
        updateUrl({ q: query || (place.lat.toFixed(4) + ',' + place.lon.toFixed(4)) });

        // Then quietly widen the net. Someone in Kansas City needs both sides of
        // the state line, but should not wait on eight files to see a result.
        // In API mode this is a no-op -- the query already spans the country.
        ChurchData.expandRegion(code).then(function (grew) {
          if (grew && token === searchToken) runSearch(token, true);
        });
      });
    }).catch(function (error) {
      if (token === searchToken) setStatus(error.message, 'error');
    });
  }

  function browseState(entry) {
    var token = ++searchToken;
    state.mode = 'state';
    state.origin = null;
    state.stateCode = entry.code;
    // Alphabetical over a whole state opens on "638" and "2nda Iglesia" and
    // buries everything usable. Rank by what is known instead.
    state.sort = 'complete';
    dom.sort.value = 'complete';
    dom.radius.closest('.filter-block').hidden = true;   // meaningless for a whole state

    setStatus('Loading churches in ' + entry.name + '…');

    // Returned so a deep link can wait for the list before opening a church on
    // top of it -- otherwise Back lands on an empty page.
    return ChurchData.prepare(entry.code).then(function () {
      if (token !== searchToken) return null;
      return runSearch(token, true).then(function () {
        setStatus('Showing churches in ' + entry.name + '.', 'ok');
        updateUrl({ state: entry.code });
      });
    }).catch(function (error) {
      if (token === searchToken) setStatus(error.message, 'error');
    });
  }

  function buildParams(offset) {
    var filters = state.filters;
    var params = {
      q: filters.name,
      families: filters.families,
      denominations: filters.denominations,
      hasWebsite: filters.hasWebsite, hasPhone: filters.hasPhone,
      hasServices: filters.hasServices, wheelchair: filters.wheelchair,
      hearingLoop: filters.hearingLoop,
      serviceDays: filters.serviceDays, servicePeriods: filters.servicePeriods,
      churchmanship: filters.churchmanship,
      sort: state.sort, limit: PAGE_SIZE, offset: offset || 0
    };
    if (state.mode === 'near' && state.origin) {
      params.lat = state.origin.lat;
      params.lon = state.origin.lon;
      params.radius = filters.radius;
    } else if (state.stateCode) {
      params.state = state.stateCode;
    }
    return params;
  }

  function runSearch(token, reset) {
    return ChurchData.search(buildParams(reset ? 0 : state.results.length))
      .then(function (body) {
        if (token !== searchToken) return;
        state.total = body.total;
        state.results = reset ? body.results : state.results.concat(body.results);
        reveal();
        renderCount();
        renderList();
        return renderMap(token);
      });
  }

  function rerun() {
    updateFilterToggleText();
    updateChurchmanshipNote();
    // Changing a filter is a request to see the search, not the church that
    // happens to be open. 'replace' rather than 'push' because closing as a
    // side effect of a filter change is not a navigation worth a Back stop.
    closeDetail('replace');
    if (state.mode === null) return;
    runSearch(++searchToken, true).catch(function (error) {
      setStatus(error.message, 'error');
    });
  }

  function loadMore() {
    var token = searchToken;
    dom.loadMore.disabled = true;
    ChurchData.search(buildParams(state.results.length)).then(function (body) {
      if (token !== searchToken) return;
      state.results = state.results.concat(body.results);
      renderList();
    }).catch(function (error) {
      setStatus(error.message, 'error');
    }).then(function () {
      dom.loadMore.disabled = false;
    });
  }

  function shortenPlace(label) {
    if (!label) return 'your location';
    return label.split(',').slice(0, 2).join(',').trim();
  }

  function reveal() {
    // The hero sells the site to someone who has just arrived. Once they have a
    // list it is half a screen of nothing between them and what they came for,
    // so it shrinks to the search box and stays out of the way. The box itself
    // never goes: it is how you start the next search.
    dom.hero.classList.add('is-compact');

    if (dom.layout.hidden) {
      dom.layout.hidden = false;
      ensureMap();
      // Leaflet measures its container on creation, and it was hidden until now.
      if (map) requestAnimationFrame(function () { map.invalidateSize(); });
      dom.layout.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  /* --------------------------------------------------------------- filtering */

  function toggleFamily(family, chip) {
    var index = state.filters.families.indexOf(family);
    if (index === -1) state.filters.families.push(family);
    else state.filters.families.splice(index, 1);

    var on = index === -1;
    chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    chip.classList.toggle('is-on', on);

    // Selecting a family reveals the specific denominations inside it. Drop any
    // chosen denomination that no longer belongs to a selected family.
    state.filters.denominations = state.filters.denominations.filter(function (label) {
      return familyOf(label) === null || state.filters.families.indexOf(familyOf(label)) !== -1;
    });
    renderDenominationOptions();
    // Deselecting Anglican has to clear any band still chosen, or the filter
    // stays applied from a panel nobody can see.
    renderChurchmanshipFilter();
    rerun();
  }

  function familyOf(denominationLabel) {
    var groups = (state.meta && state.meta.denominations) || {};
    var families = Object.keys(groups);
    for (var i = 0; i < families.length; i++) {
      var found = groups[families[i]].some(function (entry) {
        return entry.label === denominationLabel;
      });
      if (found) return families[i];
    }
    return null;
  }

  /* The second filter level. 481 denominations is far too many to list flat, so
     the picker only offers the ones inside the families currently selected. */
  function renderDenominationOptions() {
    var groups = (state.meta && state.meta.denominations) || {};
    var chosenFamilies = state.filters.families.filter(function (family) {
      return (groups[family] || []).length > 0;
    });

    dom.denomBlock.hidden = chosenFamilies.length === 0;
    dom.denomSelect.textContent = '';
    dom.denomSelect.appendChild(el('option', { value: '', text: 'Add a denomination…' }));

    chosenFamilies.forEach(function (family) {
      var label = (state.meta.families.filter(function (f) { return f.key === family; })[0] || {}).label;
      var group = el('optgroup', { label: label || family });
      groups[family].forEach(function (entry) {
        if (state.filters.denominations.indexOf(entry.label) !== -1) return;
        group.appendChild(el('option', {
          value: entry.label,
          text: entry.label + ' (' + formatNumber(entry.count) + ')'
        }));
      });
      if (group.children.length) dom.denomSelect.appendChild(group);
    });

    dom.denomChosen.textContent = '';
    state.filters.denominations.forEach(function (label) {
      var pill = el('button', {
        type: 'button', class: 'chip is-on',
        'aria-label': 'Remove the ' + label + ' filter'
      }, [el('span', { text: label }), el('span', { class: 'chip-x', text: '×' })]);
      pill.addEventListener('click', function () { removeDenomination(label); });
      dom.denomChosen.appendChild(pill);
    });
  }

  function addDenomination(label) {
    if (!label || state.filters.denominations.indexOf(label) !== -1) return;
    state.filters.denominations.push(label);
    renderDenominationOptions();
    rerun();
  }

  function removeDenomination(label) {
    var index = state.filters.denominations.indexOf(label);
    if (index === -1) return;
    state.filters.denominations.splice(index, 1);
    renderDenominationOptions();
    rerun();
  }

  // The filter panel is a full screen tall on a phone, and it sits above both
  // the map and the results, so on a narrow viewport it starts collapsed. The
  // markup ships expanded and this is the only thing that ever collapses it:
  // if the script fails, the filters are still reachable.
  var NARROW = '(max-width: 720px)';

  function setUpFilterCollapse() {
    if (!dom.filtersToggle || !dom.filtersBody) return;
    dom.filtersToggle.hidden = false;
    dom.filtersToggle.addEventListener('click', function () {
      setFiltersOpen(dom.filtersToggle.getAttribute('aria-expanded') !== 'true');
    });

    var narrow = window.matchMedia(NARROW);
    // Collapsed only while the viewport is narrow. Widening re-opens it, so a
    // rotated phone or a resized window never leaves the sidebar shut with no
    // visible way to open it -- the toggle is hidden above 720px.
    var apply = function () { setFiltersOpen(!narrow.matches); };
    apply();
    if (narrow.addEventListener) narrow.addEventListener('change', apply);
    else if (narrow.addListener) narrow.addListener(apply);   // older Safari
  }

  function setFiltersOpen(open) {
    if (!dom.filtersToggle || !dom.filtersBody) return;
    dom.filtersBody.hidden = !open;
    dom.filtersToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    updateFilterToggleText();
  }

  function updateFilterToggleText() {
    if (!dom.filtersToggleText) return;
    var open = dom.filtersToggle.getAttribute('aria-expanded') === 'true';
    var active = countActiveFilters();
    // When the panel is shut the chips and pills go with it, so the count is
    // the only thing telling you a filter is narrowing your results.
    dom.filtersToggleText.textContent = open
      ? 'Hide filters'
      : active
        ? 'Filters (' + active + ' active)'
        : 'Filters';
  }

  function countActiveFilters() {
    var f = state.filters;
    var n = f.families.length + f.denominations.length +
            f.serviceDays.length + f.servicePeriods.length;
    if (f.name) n += 1;
    ['hasWebsite', 'hasPhone', 'hasServices', 'wheelchair', 'hearingLoop']
      .forEach(function (key) { if (f[key]) n += 1; });
    if (state.mode === 'near' && f.radius !== 25) n += 1;
    return n;
  }

  function resetFilters() {
    state.filters = {
      radius: 25, name: '', families: [], denominations: [],
      hasWebsite: false, hasPhone: false, hasServices: false, wheelchair: false,
      hearingLoop: false, serviceDays: [], servicePeriods: [], churchmanship: []
    };
    dom.radius.value = 25;
    dom.radiusOut.textContent = '25';
    dom.nameFilter.value = '';
    ['website', 'phone', 'services', 'wheelchair', 'hearing'].forEach(function (key) {
      dom[key].checked = false;
    });
    [dom.chips, dom.serviceDays, dom.servicePeriods].forEach(function (group) {
      Array.prototype.forEach.call(group.children, function (chip) {
        chip.setAttribute('aria-pressed', 'false');
        chip.classList.remove('is-on');
      });
    });
    renderDenominationOptions();
    rerun();
  }

  /* ----------------------------------------------------------------- results */

  function renderCount() {
    if (!state.total) {
      dom.count.textContent = 'No churches match these filters';
      return;
    }
    var suffix = state.mode === 'near'
      ? ' within ' + state.filters.radius + ' miles'
      : ' in ' + stateNameFor(state.stateCode);
    dom.count.textContent = formatNumber(state.total) +
      (state.total === 1 ? ' church' : ' churches') + suffix;
  }

  function stateNameFor(code) {
    var match = ((state.meta && state.meta.states) || []).filter(function (s) {
      return s.code === code;
    })[0];
    return match ? match.name : code;
  }

  function badge(church) {
    return el('span', {
      class: 'badge fam-' + (church.denomination ? church.family : 'unknown'),
      text: church.denomination || 'Denomination not listed'
    });
  }

  /* Returns '' when nothing but the state is known -- a card reading just "KS"
     is worse than one that admits the address is missing. */
  function addressLine(church) {
    if (!church.address && !church.city) return '';
    var parts = [church.address, church.city, church.state].filter(Boolean);
    if (church.postcode) parts.push(church.postcode);
    return parts.join(', ');
  }

  function directionsUrl(church) {
    return 'https://www.openstreetmap.org/directions?to=' + church.lat + '%2C' + church.lon;
  }

  function saveButton(church) {
    if (!ChurchAccount.available()) return null;

    var saved = ChurchAccount.isSaved(church.id);
    var button = el('button', {
      type: 'button',
      class: 'action action-save' + (saved ? ' is-saved' : ''),
      'aria-pressed': saved ? 'true' : 'false',
      'aria-label': (saved ? 'Remove ' : 'Save ') + church.name +
                    (saved ? ' from your saved churches' : ' to your saved churches'),
      text: saved ? '★ Saved' : '☆ Save'
    });

    button.addEventListener('click', function () {
      if (!ChurchAccount.user()) {
        ChurchAccount.open('signin', 'Sign in to keep a list of churches.');
        return;
      }
      button.disabled = true;
      ChurchAccount.toggleSave(church).catch(function (error) {
        toast(error.message);
      }).then(function () {
        button.disabled = false;
      });
    });
    return button;
  }

  function buildCard(church) {
    var actions = [];

    var site = safeUrl(church.website);
    if (site) actions.push(el('a', {
      class: 'action', href: site, rel: 'noopener nofollow', target: '_blank', text: 'Website'
    }));

    var tel = telHref(church.phone);
    if (tel) actions.push(el('a', { class: 'action', href: tel, text: church.phone }));

    actions.push(el('a', {
      class: 'action', href: directionsUrl(church), rel: 'noopener', target: '_blank',
      text: 'Directions'
    }));

    // Clicking the card also does this, but a mouse-only affordance is no
    // affordance at all, and the card cannot be a button because it holds links.
    if (!mapUnavailable) {
      var locate = el('button', {
        type: 'button', class: 'action',
        'aria-label': 'Show ' + church.name + ' on the map', text: 'Show on map'
      });
      locate.addEventListener('click', function () { focusChurch(church.id); });
      actions.push(locate);
    }

    var save = saveButton(church);
    if (save) actions.push(save);

    if (church.family === 'anglican' && ChurchAccount.available()) {
      var cm = el('button', {
        type: 'button', class: 'action',
        'aria-label': 'Churchmanship of ' + church.name, text: 'Churchmanship'
      });
      cm.addEventListener('click', function () { ChurchManship.open(church); });
      actions.push(cm);
    }

    if (ChurchAccount.available()) {
      var reviews = el('button', {
        type: 'button', class: 'action',
        'aria-label': 'Reviews of ' + church.name, text: 'Reviews'
      });
      reviews.addEventListener('click', function () { ChurchReviews.open(church); });
      actions.push(reviews);
    }

    var meta = [];
    if (church.service_text || church.services) {
      // Where a time came from is part of the time. OSM's is an explicit tag;
      // the website reading is inferred from prose and can be a summer schedule
      // nobody took down. Turning up to a locked door is the cost of hiding that.
      meta.push(el('p', { class: 'services' }, [
        'Services: ' + (church.service_text || church.services),
        church.service_source === 'website'
          ? el('span', {
              class: 'source-note' + (serviceIsStale(church) ? ' is-stale' : ''),
              text: ' — ' + serviceProvenance(church)
            })
          : null
      ]));
    }
    else if (church.hours) meta.push(el('p', { class: 'services', text: 'Open: ' + church.hours }));
    var access = [];
    if (church.wheelchair === 'yes') access.push('♿ Wheelchair accessible');
    if (church.hearing_loop === 'yes') access.push('👂 Hearing loop');
    if (access.length) meta.push(el('p', { class: 'access', text: access.join(' · ') }));
    if (church.updated) {
      // A record nobody has touched in years deserves a quieter presentation
      // than one edited last month.
      var year = parseInt(church.updated.slice(0, 4), 10);
      if (year && year <= new Date().getFullYear() - 4) {
        meta.push(el('p', { class: 'stale',
          text: 'Last checked in OpenStreetMap ' + church.updated.slice(0, 4) + ' — call ahead' }));
      }
    }
    if (church.note) meta.push(el('p', { class: 'note', text: 'Your note: ' + church.note }));

    // The inline meter renders only when there is an estimate; an empty meter
    // would read as "middle", which is a claim we have not earned.
    if (church.family === 'anglican' && church.cm_confidence) {
      var inline = ChurchManship.meter({
        known: true,
        ceremonial: church.cm_ceremonial, theology: church.cm_theology,
        ceremonialLabel: '', theologyLabel: '',
        confidence: church.cm_confidence, votes: church.cm_votes || 0,
        source: church.cm_source, scrapedSource: church.cm_scraped_source
      }, true);
      if (inline) meta.push(inline);
    }

    var address = addressLine(church);
    var aside = state.mode === 'near' && church.distance != null
      ? el('span', { class: 'distance', text: formatDistance(church.distance) })
      : el('span', { class: 'distance muted', text: church.city || '' });

    var card = el('li', { class: 'church-card', 'data-id': church.id }, [
      el('div', { class: 'card-head' }, [
        el('h3', { class: 'church-name', text: church.name }), aside
      ]),
      badge(church),
      address ? el('p', { class: 'address', text: address })
              : el('p', { class: 'address muted', text: 'Address not recorded' })
    ].concat(meta).concat([el('div', { class: 'actions' }, actions)]));

    card.addEventListener('click', function (event) {
      if (event.target.closest('a, button')) return;   // real controls handle themselves
      openChurch(church);
    });
    // The card is not a button -- it contains links -- so keyboard users need
    // this said explicitly rather than inferred from the click handler.
    card.tabIndex = 0;
    card.setAttribute('role', 'link');
    card.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      if (event.target.closest('a, button')) return;
      event.preventDefault();
      openChurch(church);
    });

    return card;
  }

  /* ------------------------------------------------------------ church detail */

  /* A card is a summary. Tapping one used to move a map pin, which on a phone
     happened off-screen and looked like nothing at all -- so a tap appeared to
     do nothing, and everything the scrape knows about a church had no place to
     be shown. This is that place: one church, everything known about it, at its
     own URL so it can be linked to and reached with the Back button. */

  var detailChurch = null;

  /* `history` is one of 'push' (a tap, which should be undoable with Back),
     'replace' (a cold deep link -- the URL is already this church, and the
     search that ran underneath rewrote it), or 'none' (reacting to popstate,
     where pushing would stack a duplicate entry). */
  function openChurch(church, historyMode) {
    detailChurch = church;
    renderDetail(church);
    dom.detail.hidden = false;
    dom.results.hidden = true;
    dom.mapPane.hidden = true;
    document.querySelector('.layout').classList.add('showing-detail');

    if (historyMode !== 'none') {
      var query = new URLSearchParams(window.location.search);
      query.set('church', church.id);
      if (church.state) query.set('state', church.state);
      var entry = { church: church.id, state: church.state };
      if (historyMode === 'replace') history.replaceState(entry, '', '?' + query.toString());
      else history.pushState(entry, '', '?' + query.toString());
    }
    document.title = church.name + ' — ChurchFind';
    dom.detail.scrollIntoView({ block: 'start' });
    dom.detailBack.focus();
  }

  function closeDetail(historyMode) {
    var wasOpen = !dom.detail.hidden;
    detailChurch = null;
    dom.detail.hidden = true;
    dom.results.hidden = false;
    dom.mapPane.hidden = mapUnavailable;
    document.querySelector('.layout').classList.remove('showing-detail');
    document.title = 'ChurchFind — find a church near you';

    if (wasOpen && historyMode !== 'none') {
      var query = new URLSearchParams(window.location.search);
      query.delete('church');
      var search = query.toString();
      var url = search ? '?' + search : window.location.pathname;
      // 'replace' when the detail was closed as a side effect of something else
      // (a filter change): the URL must stop naming a church nobody is looking
      // at, but that is not a navigation worth a Back stop of its own.
      if (historyMode === 'replace') history.replaceState({}, '', url);
      else history.pushState({}, '', url);
    }
    // Leaflet lays out against a hidden container as zero-sized, so it has to be
    // told the pane is back or the tiles come back grey and offset.
    if (map) setTimeout(function () { map.invalidateSize(); }, 0);
  }

  function detailRow(label, value, note) {
    if (!value) return null;
    return el('div', { class: 'detail-row' }, [
      el('dt', { text: label }),
      el('dd', {}, [value, note ? el('span', { class: 'source-note', text: note }) : null])
    ]);
  }

  function detailLinkRow(label, href, text, external) {
    if (!href) return null;
    var attrs = { href: href, text: text };
    if (external) { attrs.rel = 'noopener nofollow'; attrs.target = '_blank'; }
    return el('div', { class: 'detail-row' }, [
      el('dt', { text: label }), el('dd', {}, [el('a', attrs)])
    ]);
  }

  function renderDetail(church) {
    var body = dom.detailBody;
    body.textContent = '';

    body.appendChild(el('h2', { class: 'detail-name', id: 'detail-name', text: church.name }));
    body.appendChild(badge(church));

    var address = addressLine(church);
    body.appendChild(address
      ? el('p', { class: 'detail-address', text: address })
      : el('p', { class: 'detail-address muted', text: 'Address not recorded' }));

    var actions = [];
    if (!mapUnavailable && church.lat != null) {
      var locate = el('button', { type: 'button', class: 'action',
        text: 'Show on map', 'aria-label': 'Show ' + church.name + ' on the map' });
      locate.addEventListener('click', function () {
        closeDetail();
        focusChurch(church.id);
      });
      actions.push(locate);
    }
    actions.push(el('a', { class: 'action', href: directionsUrl(church),
                           rel: 'noopener', target: '_blank', text: 'Directions' }));
    var save = saveButton(church);
    if (save) actions.push(save);
    body.appendChild(el('div', { class: 'actions' }, actions));

    var facts = el('dl', { class: 'detail-facts' }, [
      detailLinkRow('Website', safeUrl(church.website), church.website, true),
      detailLinkRow('Phone', telHref(church.phone), church.phone, false),
      detailLinkRow('Email', church.email ? 'mailto:' + church.email : '', church.email, false),
      detailRow('Denomination', church.denomination || 'Not recorded'),
      detailRow('Services', church.service_text || church.services || church.hours || '',
                church.service_source === 'website'
                  ? capitalise(serviceProvenance(church)) +
                    '. Check with them before travelling.'
                  : church.service_source === 'osm'
                    ? 'From an OpenStreetMap service_times tag.' : ''),
      detailRow('Accessibility', accessibilityText(church)),
      detailRow('Last edited in OpenStreetMap', church.updated || '')
    ]);
    if (facts.children.length) body.appendChild(facts);

    body.appendChild(churchmanshipSection(church));

    if (ChurchAccount.available()) {
      var extra = [];
      var reviews = el('button', { type: 'button', class: 'btn btn-ghost', text: 'Reviews' });
      reviews.addEventListener('click', function () { ChurchReviews.open(church); });
      extra.push(reviews);
      var report = el('button', { type: 'button', class: 'btn btn-ghost',
                                  text: 'Report a correction' });
      report.addEventListener('click', function () { ChurchAccount.reportCorrection(church); });
      extra.push(report);
      body.appendChild(el('div', { class: 'detail-extra' }, extra));
    }

    body.appendChild(el('p', { class: 'detail-source' }, [
      'Record from ',
      el('a', { href: osmUrl(church), rel: 'noopener', target: '_blank',
                text: 'OpenStreetMap' }),
      ', which anyone can correct.'
    ]));
  }

  /* A scraped service time is only as good as the day it was read. Past this
     many months a parish has had a summer schedule, a Christmas, and possibly a
     new priest, so the note stops saying "from the website" and starts saying
     how long ago. Nothing is hidden -- a stale time is still the best guess
     available -- but it stops being presented as current fact. */
  var STALE_AFTER_DAYS = 180;

  function serviceAgeDays(church) {
    if (!church.service_checked) return null;
    var when = Date.parse(church.service_checked + 'T00:00:00Z');
    if (isNaN(when)) return null;
    return Math.floor((Date.now() - when) / 86400000);
  }

  function serviceIsStale(church) {
    var days = serviceAgeDays(church);
    return days !== null && days > STALE_AFTER_DAYS;
  }

  function serviceProvenance(church) {
    var days = serviceAgeDays(church);
    if (days === null) return 'from the parish website';
    if (days > STALE_AFTER_DAYS) {
      var months = Math.round(days / 30);
      return 'read from the parish website ' +
             (months >= 12 ? 'over a year ago' : months + ' months ago');
    }
    return 'from the parish website, checked ' + relativeDate(days);
  }

  function relativeDate(days) {
    if (days <= 1) return 'today';
    if (days < 14) return days + ' days ago';
    if (days < 60) return Math.round(days / 7) + ' weeks ago';
    return Math.round(days / 30) + ' months ago';
  }

  function capitalise(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function accessibilityText(church) {
    var parts = [];
    if (church.wheelchair === 'yes') parts.push('Wheelchair accessible');
    else if (church.wheelchair === 'limited') parts.push('Limited wheelchair access');
    if (church.hearing_loop === 'yes') parts.push('Hearing loop');
    if (church.toilets_wheelchair === 'yes') parts.push('Accessible toilet');
    return parts.join(' · ');
  }

  function osmUrl(church) {
    var kinds = { n: 'node', w: 'way', r: 'relation' };
    var kind = kinds[String(church.id).charAt(0)];
    return kind
      ? 'https://www.openstreetmap.org/' + kind + '/' + String(church.id).slice(1)
      : 'https://www.openstreetmap.org/';
  }

  /* Churchmanship on the detail page rather than only behind a dialog: it is
     one of the few things here the site worked out rather than copied, and
     burying it behind a button nobody presses wasted it. Voting still needs an
     account; reading does not. */
  function churchmanshipSection(church) {
    if (church.family !== 'anglican') return document.createDocumentFragment();

    var section = el('section', { class: 'detail-cm' }, [
      el('h3', { text: 'Churchmanship' })
    ]);

    if (!church.cm_confidence) {
      section.appendChild(el('p', { class: 'muted', text:
        'No estimate yet. Nothing in this parish’s website or Wikipedia article ' +
        'said enough about its worship to place it.' }));
    } else {
      var meter = ChurchManship.meter({
        known: true,
        ceremonial: church.cm_ceremonial, theology: church.cm_theology,
        ceremonialLabel: ChurchManship.label('ceremonial', church.cm_ceremonial),
        theologyLabel: ChurchManship.label('theology', church.cm_theology),
        confidence: church.cm_confidence, votes: church.cm_votes || 0,
        source: church.cm_source, scrapedSource: church.cm_scraped_source
      });
      if (meter) section.appendChild(meter);

      var evidence = (church.cm_evidence || '').split(',').filter(Boolean);
      if (evidence.length) {
        section.appendChild(el('details', { class: 'cm-evidence' }, [
          el('summary', { text: 'What this is based on' }),
          el('p', { text: 'Phrases found: ' + evidence.join(', ') + '.' }),
          el('p', { class: 'cm-caveat', text:
            'Dedications are deliberately ignored — St Mary the Virgin tells you ' +
            'about the founding decade, not about this Sunday.' })
        ]));
      }
    }

    if (ChurchAccount.available()) {
      var vote = el('button', { type: 'button', class: 'btn btn-ghost',
        text: church.cm_confidence ? 'Correct this' : 'Add a reading' });
      vote.addEventListener('click', function () { ChurchManship.open(church); });
      section.appendChild(vote);
    } else {
      section.appendChild(el('p', { class: 'muted small', text:
        'Readings come from parish websites and Wikipedia. Correcting one needs ' +
        'an account, which needs the API server — this is the static build.' }));
    }
    return section;
  }

  function renderList() {
    dom.list.textContent = '';
    var fragment = document.createDocumentFragment();
    state.results.forEach(function (church) { fragment.appendChild(buildCard(church)); });
    dom.list.appendChild(fragment);

    var remaining = state.total - state.results.length;
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
    if (typeof L === 'undefined') { disableMap(); return; }
    try {
      var tiles = ChurchData.tileConfig();
      map = L.map('map', { scrollWheelZoom: false });

      var layer = L.tileLayer(tiles.url, {
        maxZoom: 19, attribution: tiles.attribution, crossOrigin: true
      });

      // Without this the basemap just stays grey and nobody can tell whether the
      // tile host is blocked, the URL is wrong, or there is simply no data there.
      // Counting tileerror events alone proved unreliable -- a single spurious
      // tileload suppressed the warning permanently -- so the authority is
      // whether any tile has actually painted, checked once the network has had
      // time to answer.
      layer.on('tileload', hideTileWarning);
      layer.addTo(map);
      setTimeout(checkTilesPainted, 6000);
      markerLayer = L.layerGroup().addTo(map);
      map.setView([39.5, -98.35], 4);
    } catch (error) {
      disableMap();
    }
  }

  /* Authoritative test: has any tile image actually decoded? A request that
     404s, is blocked by an extension, or is refused by the tile host all end up
     the same way here -- an <img> with no pixels. */
  function checkTilesPainted() {
    if (!map) return;
    var tiles = document.querySelectorAll('#map img.leaflet-tile');
    if (!tiles.length) return;
    var painted = Array.prototype.some.call(tiles, function (img) {
      return img.complete && img.naturalWidth > 0;
    });
    if (painted) hideTileWarning();
    else showTileWarning();
  }

  /* The markers are still positioned correctly relative to each other, so the
     pane stays useful without a basemap -- this explains the grey rather than
     hiding the map entirely. */
  function showTileWarning() {
    var pane = document.querySelector('.map-pane');
    if (!pane || pane.querySelector('.map-warning')) return;
    pane.appendChild(el('div', { class: 'map-warning' }, [
      el('strong', { text: 'Background map unavailable' }),
      el('p', { text: 'Map tiles could not be loaded, so only the church markers are ' +
                      'drawn. This is usually a blocked or unreachable tile server. ' +
                      'The results list is unaffected.' }),
      el('p', { class: 'map-warning-host', text: 'Tiles requested from ' + tileHost() })
    ]));
  }

  function hideTileWarning() {
    var warning = document.querySelector('.map-warning');
    if (warning) warning.remove();
  }

  function tileHost() {
    try {
      return new URL(ChurchData.tileConfig().url).host;
    } catch (error) {
      return 'the configured tile server';
    }
  }

  function disableMap() {
    mapUnavailable = true;
    map = null;
    var pane = document.querySelector('.map-pane');
    if (pane) pane.hidden = true;
    document.querySelector('.layout').classList.add('no-map');
  }

  /* The list is paginated but the map should show more than the first page, so
     this asks for its own window of results rather than reusing state.results. */
  function renderMap(token) {
    if (!map) return Promise.resolve();

    var params = buildParams(0);
    params.limit = MAX_MARKERS;
    return ChurchData.search(params).then(function (body) {
      if (token !== searchToken || !map) return;
      drawMarkers(body.results);
      if (body.total > MAX_MARKERS) {
        toast('Map shows the closest ' + formatNumber(MAX_MARKERS) + ' of ' +
              formatNumber(body.total) + ' matches. Narrow the filters to see the rest.');
      }
    }).catch(function () { /* the list is what matters */ });
  }

  function drawMarkers(churches) {
    markerLayer.clearLayers();
    markersById = {};
    var bounds = [];

    if (state.origin && state.mode === 'near') {
      L.circleMarker([state.origin.lat, state.origin.lon], {
        radius: 8, color: '#b4791f', weight: 3, fillColor: '#f0c46a', fillOpacity: 0.9
      }).bindPopup('Your search location').addTo(markerLayer);
      bounds.push([state.origin.lat, state.origin.lon]);
    }

    churches.forEach(function (church) {
      var marker = L.circleMarker([church.lat, church.lon], {
        radius: 6, color: '#2f3f6b', weight: 2, fillColor: '#5b7ac9', fillOpacity: 0.85
      });
      marker.bindPopup(popupFor(church));
      marker.addTo(markerLayer);
      markersById[church.id] = marker;
      bounds.push([church.lat, church.lon]);
    });

    if (bounds.length > 1) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 14 });
    else if (bounds.length === 1) map.setView(bounds[0], 12);
  }

  /* Popups take a DOM node, so nothing here needs HTML escaping. */
  function popupFor(church) {
    var children = [el('strong', { text: church.name })];
    if (church.denomination) children.push(el('div', { class: 'popup-den', text: church.denomination }));
    var address = addressLine(church);
    if (address) children.push(el('div', { class: 'popup-addr', text: address }));
    if (state.mode === 'near' && church.distance != null) {
      children.push(el('div', { class: 'popup-dist', text: formatDistance(church.distance) + ' away' }));
    }
    var site = safeUrl(church.website);
    if (site) children.push(el('a', {
      href: site, target: '_blank', rel: 'noopener nofollow', text: 'Visit website'
    }));
    return el('div', { class: 'popup' }, children);
  }

  function focusChurch(id) {
    if (mapUnavailable) return;
    var marker = markersById[id];
    if (!marker) { toast('That church is outside the range shown on the map.'); return; }
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 14));
    marker.openPopup();
    // On a phone the map is above the list, so "show on map" used to move a pin
    // on a pane that was off-screen -- indistinguishable from the button doing
    // nothing at all. Scroll to whatever was just changed.
    if (dom.mapPane && !dom.mapPane.hidden) {
      dom.mapPane.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    document.querySelectorAll('.church-card.is-active').forEach(function (node) {
      node.classList.remove('is-active');
    });
    var card = dom.list.querySelector('[data-id="' + CSS.escape(id) + '"]');
    if (card) card.classList.add('is-active');
  }

  /* ------------------------------------------------------------ saved churches */

  function showSaved() {
    if (!ChurchAccount.user()) {
      ChurchAccount.open('signin', 'Sign in to see the churches you have saved.');
      return;
    }
    var token = ++searchToken;
    ChurchAccount.listSaved().then(function (body) {
      if (token !== searchToken) return;
      state.mode = 'saved';
      state.origin = null;
      state.stateCode = null;
      state.results = body.results;
      state.total = body.total;

      reveal();
      dom.count.textContent = body.total
        ? formatNumber(body.total) + (body.total === 1 ? ' saved church' : ' saved churches')
        : 'You have not saved any churches yet';
      renderList();
      if (map) { drawMarkers(body.results.slice(0, MAX_MARKERS)); }
      dom.loadMore.hidden = true;
      setStatus('Showing your saved churches.', 'ok');
      updateUrl({ view: 'saved' });
    }).catch(function (error) {
      setStatus(error.message, 'error');
    });
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

  function stateEntry(code) {
    if (!code) return null;
    return ((state.meta && state.meta.states) || []).filter(function (s) {
      return s.code === code.toUpperCase();
    })[0] || null;
  }

  function restoreFromUrl() {
    var params = new URLSearchParams(window.location.search);

    if (params.get('view') === 'saved' && ChurchAccount.user()) { showSaved(); return; }

    var stateParam = params.get('state');

    // A shared link to one church. The state search still runs underneath so
    // that Back from the detail lands on a populated list rather than an empty
    // page -- and in static mode it is what puts the state file in memory.
    var churchParam = params.get('church');
    if (churchParam) {
      var entry = stateEntry(stateParam);
      var ready = entry ? browseState(entry) : Promise.resolve();
      Promise.resolve(ready).catch(function () {}).then(function () {
        return ChurchData.getChurch(churchParam, stateParam);
      }).then(function (church) {
        // 'replace': the search that just ran rewrote the URL and dropped the
        // church param, so this puts it back without adding a history entry.
        if (church) openChurch(church, 'replace');
        else setStatus('That church could not be found.', 'error');
      }).catch(function (error) { setStatus(error.message, 'error'); });
      return;
    }

    if (stateParam) {
      var known = stateEntry(stateParam);
      if (known) { browseState(known); return; }
    }

    var query = params.get('q');
    if (query) {
      dom.input.value = query;
      searchByQuery(query);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
