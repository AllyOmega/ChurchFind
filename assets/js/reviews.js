/* Reviews: the per-church dialog, and the moderation queue for moderators.
 *
 * Nothing here decides what is publishable -- the server does that before a row
 * is written. This file's only job on that front is to be honest with the
 * author about what happened to their review, including when it is being held,
 * because a post that silently vanishes is worse than one that says why.
 *
 * Loads after account.js and exposes ChurchReviews.
 */
(function (global) {
  'use strict';

  var API = 'api';
  var MAX_BODY = 4000;

  var dialog = null;
  var currentChurch = null;

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

  function readCookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function request(method, path, body) {
    var options = { method: method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (method !== 'GET') {
      options.headers['X-CSRF-Token'] = readCookie('cf_csrf');
      if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
    }
    return fetch(API + path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          var detail = payload.detail;
          if (Array.isArray(detail)) detail = (detail[0] || {}).msg || 'That did not work.';
          throw new Error(detail || 'That did not work.');
        }
        return payload;
      });
    });
  }

  /* ------------------------------------------------------------- the dialog */

  function build() {
    if (dialog) return dialog;
    dialog = el('dialog', { class: 'review-dialog' });
    dialog.innerHTML = [
      '<form method="dialog" class="auth-close-form">',
      '  <button value="cancel" class="auth-close" aria-label="Close">&times;</button>',
      '</form>',
      '<h2 class="review-title"></h2>',
      '<p class="review-summary"></p>',
      '<div class="review-list"></div>',
      '<div class="review-compose"></div>'
    ].join('');
    document.body.appendChild(dialog);
    return dialog;
  }

  function open(church) {
    if (!ChurchAccount.available()) return;
    currentChurch = church;
    build();
    dialog.querySelector('.review-title').textContent = church.name;
    dialog.querySelector('.review-summary').textContent = 'Loading reviews…';
    dialog.querySelector('.review-list').textContent = '';
    dialog.querySelector('.review-compose').textContent = '';
    dialog.showModal();
    refresh();
  }

  function refresh() {
    return request('GET', '/churches/' + encodeURIComponent(currentChurch.id) + '/reviews')
      .then(render)
      .catch(function (error) {
        dialog.querySelector('.review-summary').textContent = error.message;
      });
  }

  var STATUS_NOTE = {
    pending: 'Awaiting moderation — only you can see this.',
    escalated: 'Held for a moderator to look at — only you can see this.',
    rejected: 'Not published because it does not meet the review guidelines.'
  };

  function render(body) {
    var summary = dialog.querySelector('.review-summary');
    summary.textContent = body.total
      ? body.average + ' out of 5 from ' + body.total +
        (body.total === 1 ? ' review' : ' reviews')
      : 'No published reviews yet.';

    var list = dialog.querySelector('.review-list');
    list.textContent = '';
    body.results.forEach(function (review) {
      var note = review.mine ? STATUS_NOTE[review.status] : null;
      var card = el('article', { class: 'review' + (review.mine ? ' is-mine' : '') }, [
        el('div', { class: 'review-head' }, [
          el('span', { class: 'review-stars', 'aria-label': review.rating + ' out of 5',
                       text: '★'.repeat(review.rating) + '☆'.repeat(5 - review.rating) }),
          el('span', { class: 'review-author', text: review.author })
        ]),
        el('p', { class: 'review-body', text: review.body }),
        note ? el('p', { class: 'review-status', text: note }) : null
      ]);
      if (review.mine) {
        var remove = el('button', { type: 'button', class: 'link-btn', text: 'Delete' });
        remove.addEventListener('click', function () {
          request('DELETE', '/reviews/' + review.id).then(refresh);
        });
        card.appendChild(remove);
      }
      list.appendChild(card);
    });

    renderCompose(body.results.some(function (r) { return r.mine; }));
  }

  function renderCompose(alreadyWrote) {
    var host = dialog.querySelector('.review-compose');
    host.textContent = '';

    if (!ChurchAccount.user()) {
      var prompt = el('button', { type: 'button', class: 'btn btn-primary',
                                  text: 'Sign in to write a review' });
      prompt.addEventListener('click', function () {
        dialog.close();
        ChurchAccount.open('signin', 'Sign in to review a church.');
      });
      host.appendChild(prompt);
      return;
    }

    var form = el('form', { class: 'review-form' });
    var rating = el('select', { id: 'review-rating', 'aria-label': 'Star rating' });
    [5, 4, 3, 2, 1].forEach(function (value) {
      rating.appendChild(el('option', { value: value, text: value + ' star' + (value > 1 ? 's' : '') }));
    });
    var body = el('textarea', {
      id: 'review-body', rows: '4', maxlength: String(MAX_BODY),
      placeholder: 'What was your experience of this congregation?'
    });
    var error = el('p', { class: 'auth-error' });
    var submit = el('button', { type: 'submit', class: 'btn btn-primary',
                                text: alreadyWrote ? 'Update my review' : 'Post review' });

    form.appendChild(el('label', { for: 'review-rating', text: 'Rating' }));
    form.appendChild(rating);
    form.appendChild(el('label', { for: 'review-body', text: 'Your review' }));
    form.appendChild(body);
    form.appendChild(el('p', { class: 'auth-hint',
      text: 'Reviews are moderated before they appear. Criticism is fine; abuse and ' +
            'accusations about named individuals are not.' }));
    form.appendChild(error);
    form.appendChild(submit);

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      error.textContent = '';
      submit.disabled = true;
      request('POST', '/reviews', {
        church_id: currentChurch.id,
        rating: Number(rating.value),
        body: body.value
      }).then(function (result) {
        body.value = '';
        return refresh().then(function () {
          dialog.querySelector('.review-summary').textContent = result.message;
        });
      }).catch(function (err) {
        error.textContent = err.message;
      }).then(function () {
        submit.disabled = false;
      });
    });

    host.appendChild(form);
  }

  /* -------------------------------------------------------- moderation queue */

  function openQueue() {
    build();
    currentChurch = { id: '', name: 'Moderation queue' };
    dialog.querySelector('.review-title').textContent = 'Moderation queue';
    dialog.querySelector('.review-compose').textContent = '';
    dialog.querySelector('.review-list').textContent = '';
    dialog.querySelector('.review-summary').textContent = 'Loading…';
    dialog.showModal();

    request('GET', '/moderation/queue').then(function (body) {
      dialog.querySelector('.review-summary').textContent = body.total
        ? body.total + ' review(s) waiting' +
          (body.moderationAvailable ? '' : ' — automatic moderation is not configured, ' +
           'so everything lands here')
        : 'Nothing waiting.';

      var list = dialog.querySelector('.review-list');
      list.textContent = '';
      body.results.forEach(function (review) {
        var card = el('article', { class: 'review review-queued' }, [
          el('div', { class: 'review-head' }, [
            el('span', { class: 'review-stars',
                         text: '★'.repeat(review.rating) + '☆'.repeat(5 - review.rating) }),
            el('span', { class: 'review-author', text: review.authorEmail })
          ]),
          el('p', { class: 'review-body', text: review.body }),
          el('p', { class: 'review-verdict', text:
            'Model: ' + (review.moderation.verdict || 'not run') +
            (review.moderation.categories.length
              ? ' (' + review.moderation.categories.join(', ') + ')' : '') +
            ' — ' + (review.moderation.reason || 'no reason recorded') })
        ]);

        ['approved', 'rejected'].forEach(function (decision) {
          var button = el('button', {
            type: 'button', class: 'btn btn-small review-decide',
            text: decision === 'approved' ? 'Publish' : 'Reject'
          });
          button.addEventListener('click', function () {
            button.disabled = true;
            request('POST', '/moderation/reviews/' + review.id, { status: decision })
              .then(function () { card.remove(); })
              .catch(function (error) { button.disabled = false; alert(error.message); });
          });
          card.appendChild(button);
        });
        list.appendChild(card);
      });
    }).catch(function (error) {
      dialog.querySelector('.review-summary').textContent = error.message;
    });
  }

  global.ChurchReviews = { open: open, openQueue: openQueue };
})(window);

/* ------------------------------------------------------------ churchmanship */

/* Two axes, because one line from "low" to "high" collapses things that vary
 * independently -- a Prayer Book parish can be high-ceremonial and firmly
 * Protestant, and one axis has to lie about one of those.
 *
 * Everything here is hedged on purpose. The estimate comes from reading a
 * parish's own website, which is weak evidence, so the meter shows its
 * confidence, names the phrases that produced it, and invites correction.
 */
(function (global) {
  'use strict';

  var AXES = [
    { key: 'ceremonial', label: 'Ceremonial', low: 'Low church', high: 'High church' },
    { key: 'theology', label: 'Tradition', low: 'Evangelical', high: 'Anglo-Catholic' }
  ];
  var STEPS = [
    { value: -1, text: 'Strongly' }, { value: -0.5, text: 'Somewhat' },
    { value: 0, text: 'Middle' },
    { value: 0.5, text: 'Somewhat' }, { value: 1, text: 'Strongly' }
  ];

  var dialog = null;
  var church = null;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'class') node.className = attrs[k];
      else if (k === 'text') node.textContent = attrs[k];
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) {
        node.setAttribute(k, attrs[k]);
      }
    });
    (children || []).forEach(function (c) {
      if (c) node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  function readCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }

  function request(method, path, body) {
    var options = { method: method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (method !== 'GET') {
      options.headers['X-CSRF-Token'] = readCookie('cf_csrf');
      if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
    }
    return fetch('api' + path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          var detail = payload.detail;
          if (Array.isArray(detail)) detail = (detail[0] || {}).msg || 'That did not work.';
          throw new Error(detail || 'That did not work.');
        }
        return payload;
      });
    });
  }

  /* The inline meter on a search-result card. Deliberately shows nothing at all
     when there is no estimate -- an empty meter reads as "middle", which is a
     claim we have not earned. */
  function meter(data, compact) {
    if (!data || !data.known) return null;
    var node = el('div', { class: 'cm-meter' + (compact ? ' cm-compact' : '') });

    AXES.forEach(function (axis) {
      var value = data[axis.key];
      if (value === null || value === undefined) return;
      var percent = ((value + 1) / 2) * 100;
      var marker = el('span', { class: 'cm-fill' });
      // Set through CSSOM, not a style attribute: the CSP is style-src 'self'
      // with no unsafe-inline, which blocks the attribute but not this.
      marker.style.left = percent.toFixed(1) + '%';
      node.appendChild(el('div', { class: 'cm-axis' }, [
        el('span', { class: 'cm-axis-label', text: axis.label }),
        el('div', { class: 'cm-track', role: 'img',
                    'aria-label': axis.label + ': ' + data[axis.key + 'Label'] +
                                  ' (' + axis.low + ' to ' + axis.high + ')' }, [marker]),
        el('span', { class: 'cm-value', text: data[axis.key + 'Label'] || '' })
      ]));
    });

    var strength = data.confidence >= 0.5 ? 'reasonably confident'
      : data.confidence >= 0.25 ? 'a rough guess' : 'a very rough guess';
    node.appendChild(el('p', { class: 'cm-confidence', text:
      data.votes
        ? strength + ', from ' + data.votes + (data.votes === 1 ? ' submission' : ' submissions') +
          (data.source === 'community' ? '' : ' and ' + scrapedSourceName(data))
        : strength + ', from ' + scrapedSourceName(data) + ', with no submissions yet' }));
    return node;
  }

  // Naming the source is the point: a parish describing its own worship and an
  // encyclopedia article about the building are different kinds of claim, and
  // the second is likelier to be describing how things were decades ago.
  var SOURCE_NAMES = {
    'website': 'the parish website',
    'wikipedia': 'the Wikipedia article',
    'website+wikipedia': 'the parish website and Wikipedia'
  };

  function scrapedSourceName(data) {
    return SOURCE_NAMES[data.scrapedSource] || 'the parish website';
  }

  function open(target) {
    church = target;
    build();
    dialog.querySelector('.cm-title').textContent = church.name;
    dialog.querySelector('.cm-body').textContent = 'Loading…';
    dialog.showModal();
    refresh();
  }

  function build() {
    if (dialog) return dialog;
    dialog = el('dialog', { class: 'review-dialog cm-dialog' });
    dialog.innerHTML = [
      '<form method="dialog" class="auth-close-form">',
      '  <button value="cancel" class="auth-close" aria-label="Close">&times;</button>',
      '</form>',
      '<h2 class="cm-title"></h2>',
      '<p class="cm-intro">Churchmanship on two axes. These are estimates, not labels the ',
      'parish chose for itself — if you know it, please correct them.</p>',
      '<div class="cm-body"></div>'
    ].join('');
    document.body.appendChild(dialog);
    return dialog;
  }

  function refresh() {
    return request('GET', '/churchmanship/' + encodeURIComponent(church.id)).then(render);
  }

  function render(data) {
    var host = dialog.querySelector('.cm-body');
    host.textContent = '';

    if (data.known) {
      host.appendChild(meter(data));
      if (data.evidence && data.evidence.length) {
        host.appendChild(el('details', { class: 'cm-evidence' }, [
          el('summary', { text: 'What this is based on' }),
          el('p', { text: 'Phrases found on ' + scrapedSourceName(data) + ': ' +
            data.evidence.join(', ') + '.' }),
          el('p', { class: 'cm-caveat', text:
            'Dedications are deliberately ignored — St Mary the Virgin tells you about ' +
            'the founding decade, not about this Sunday.' }),
          data.scrapedSource && data.scrapedSource.indexOf('wikipedia') !== -1
            ? el('p', { class: 'cm-caveat', text:
                'Wikipedia articles describe buildings and history more often than ' +
                'worship, and may be years out of date, so they count for less here.' })
            : null
        ]));
      }
    } else {
      host.appendChild(el('p', { class: 'cm-none', text:
        'No estimate yet. Nothing was found on a website or in a Wikipedia ' +
        'article for this parish, so the first person to answer sets it.' }));
    }

    if (!ChurchAccount.user()) {
      var prompt = el('button', { type: 'button', class: 'btn btn-primary',
                                  text: 'Sign in to add your reading' });
      prompt.addEventListener('click', function () {
        dialog.close();
        ChurchAccount.open('signin', 'Sign in to add your reading of a parish.');
      });
      host.appendChild(prompt);
      return;
    }

    var form = el('form', { class: 'cm-form' });
    var inputs = {};
    AXES.forEach(function (axis) {
      var select = el('select', { id: 'cm-' + axis.key });
      STEPS.forEach(function (step) {
        var text = step.value === 0 ? 'Middle'
          : step.text + ' ' + (step.value < 0 ? axis.low : axis.high).toLowerCase();
        select.appendChild(el('option', { value: String(step.value), text: text }));
      });
      select.value = data.mine ? String(nearestStep(data.mine[axis.key])) : '0';
      inputs[axis.key] = select;
      form.appendChild(el('label', { for: 'cm-' + axis.key, text: axis.label }));
      form.appendChild(select);
    });

    var error = el('p', { class: 'auth-error' });
    var submit = el('button', { type: 'submit', class: 'btn btn-primary',
                                text: data.mine ? 'Update my reading' : 'Submit' });
    form.appendChild(error);
    form.appendChild(submit);

    if (data.mine) {
      var withdraw = el('button', { type: 'button', class: 'link-btn', text: 'Withdraw mine' });
      withdraw.addEventListener('click', function () {
        request('DELETE', '/churchmanship/' + encodeURIComponent(church.id)).then(refresh);
      });
      form.appendChild(withdraw);
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      error.textContent = '';
      submit.disabled = true;
      request('POST', '/churchmanship', {
        church_id: church.id,
        ceremonial: Number(inputs.ceremonial.value),
        theology: Number(inputs.theology.value)
      }).then(refresh)
        .catch(function (err) { error.textContent = err.message; })
        .then(function () { submit.disabled = false; });
    });

    host.appendChild(form);
  }

  function nearestStep(value) {
    return STEPS.reduce(function (best, step) {
      return Math.abs(step.value - value) < Math.abs(best - value) ? step.value : best;
    }, 0);
  }

  global.ChurchManship = { open: open, meter: meter };
})(window);
