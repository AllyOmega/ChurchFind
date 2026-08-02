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
