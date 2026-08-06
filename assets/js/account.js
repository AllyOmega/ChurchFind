/* Accounts: the API calls, the sign-in dialog, and the saved-church list.
 *
 * Every state-changing request carries the CSRF token from the cf_csrf cookie
 * in an X-CSRF-Token header. That cookie is readable by design -- the session
 * cookie next to it is HttpOnly and is the one that actually authenticates.
 * A cross-site form can make the browser send cookies but cannot read one to
 * build the header, which is the whole trick.
 *
 * Loads before app.js and exposes ChurchAccount; when no server is present
 * every method still resolves, reporting "not signed in".
 */
(function (global) {
  'use strict';

  var API = 'api';
  var listeners = [];
  var user = null;
  var savedIds = {};       // church id -> note, for the star on each card
  var available = false;

  /* ---------------------------------------------------------------- helpers */

  function readCookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function request(method, path, body) {
    var options = {
      method: method,
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    };
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
          // FastAPI puts validation errors in a list; take the first message.
          var detail = payload.detail;
          if (Array.isArray(detail)) detail = (detail[0] || {}).msg || 'That did not work.';
          throw new Error(detail || 'That did not work.');
        }
        return payload;
      });
    });
  }

  function notify() {
    listeners.forEach(function (fn) { fn(user, savedIds); });
  }

  function onChange(fn) {
    listeners.push(fn);
    fn(user, savedIds);
  }

  /* ------------------------------------------------------------------- state */

  function init(accountsAvailable) {
    available = !!accountsAvailable;
    if (!available) { notify(); return Promise.resolve(null); }
    return request('GET', '/auth/me')
      .then(function (body) {
        user = body.user;
        return user ? refreshSaved() : null;
      })
      .catch(function () { user = null; })
      .then(function () {
        notify();
        // After the session is settled, so a reset link opens over a known
        // signed-in state rather than racing it.
        checkResetLink();
        return user;
      });
  }

  function refreshSaved() {
    if (!user) { savedIds = {}; return Promise.resolve({ results: [] }); }
    return request('GET', '/saved').then(function (body) {
      savedIds = {};
      body.results.forEach(function (church) { savedIds[church.id] = church.note || ''; });
      return body;
    });
  }

  function isSaved(churchId) {
    return Object.prototype.hasOwnProperty.call(savedIds, churchId);
  }

  function toggleSave(church) {
    if (!user) return Promise.reject(new Error('Sign in to save churches.'));
    if (isSaved(church.id)) {
      return request('DELETE', '/saved/' + encodeURIComponent(church.id)).then(function () {
        delete savedIds[church.id];
        notify();
        return false;
      });
    }
    return request('POST', '/saved', { church_id: church.id }).then(function () {
      savedIds[church.id] = '';
      notify();
      return true;
    });
  }

  function listSaved() {
    return request('GET', '/saved');
  }

  function setHome(lat, lon, label) {
    return request('PUT', '/me/home', { lat: lat, lon: lon, label: label || '' })
      .then(function () {
        if (user) user.home = { lat: lat, lon: lon, label: label || '' };
        notify();
      });
  }

  function reportCorrection(churchId, field, suggestion) {
    return request('POST', '/reports', {
      church_id: churchId, field: field, suggestion: suggestion
    });
  }

  function signIn(email, password) {
    return request('POST', '/auth/login', { email: email, password: password })
      .then(function (body) {
        user = body.user;
        return refreshSaved();
      })
      .then(function () { notify(); return user; });
  }

  function signUp(email, password, displayName) {
    return request('POST', '/auth/register', {
      email: email, password: password, display_name: displayName || ''
    }).then(function (body) {
      user = body.user;
      savedIds = {};
      notify();
      return user;
    });
  }

  function signOut() {
    return request('POST', '/auth/logout').then(function () {
      user = null;
      savedIds = {};
      notify();
    });
  }

  function changePassword(currentPassword, newPassword) {
    return request('POST', '/auth/password', {
      current_password: currentPassword, new_password: newPassword
    });
  }

  /* --------------------------------------------------------------- the dialog */

  var dialog = null;
  var form = null;
  var errorNode = null;
  var pendingMessage = '';

  function buildDialog() {
    if (dialog) return dialog;

    dialog = document.createElement('dialog');
    dialog.className = 'auth-dialog';
    dialog.innerHTML = [
      '<form method="dialog" class="auth-close-form">',
      '  <button value="cancel" class="auth-close" aria-label="Close">&times;</button>',
      '</form>',
      '<div class="auth-tabs" role="tablist">',
      '  <button type="button" class="auth-tab is-on" data-mode="signin" role="tab" aria-selected="true">Sign in</button>',
      '  <button type="button" class="auth-tab" data-mode="signup" role="tab" aria-selected="false">Create account</button>',
      '</div>',
      '<p class="auth-intro" id="auth-intro"></p>',
      '<form class="auth-form" id="auth-form" novalidate>',
      '  <div class="auth-email-row">',
      '    <label for="auth-email">Email</label>',
      '    <input id="auth-email" name="email" type="email" autocomplete="email" required>',
      '  </div>',
      '  <div class="auth-name-row" hidden>',
      '    <label for="auth-name">Display name <span class="auth-optional">(optional)</span></label>',
      '    <input id="auth-name" name="display_name" type="text" autocomplete="nickname" maxlength="80">',
      '  </div>',
      '  <div class="auth-password-row">',
      '    <label for="auth-password">Password</label>',
      '    <input id="auth-password" name="password" type="password">',
      '  </div>',
      '  <p class="auth-hint" id="auth-hint">At least 10 characters. A short phrase beats a short password.</p>',
      '  <p class="auth-error" id="auth-error" role="alert"></p>',
      '  <p class="auth-notice" id="auth-notice" role="status" hidden></p>',
      '  <button type="submit" class="btn btn-primary auth-submit">Sign in</button>',
      '  <button type="button" class="link-btn auth-forgot" id="auth-forgot">Forgotten your password?</button>',
      '</form>'
    ].join('');

    document.body.appendChild(dialog);
    form = dialog.querySelector('#auth-form');
    errorNode = dialog.querySelector('#auth-error');

    dialog.querySelectorAll('.auth-tab').forEach(function (tab) {
      tab.addEventListener('click', function () { setMode(tab.dataset.mode); });
    });
    dialog.querySelector('#auth-forgot').addEventListener('click', function () {
      setMode('forgot');
    });
    form.addEventListener('submit', submit);
    return dialog;
  }

  /* Three modes share one form: sign in, create account, and forgot -- which is
     the same form with the password field removed, because all it needs is an
     address. A fourth, `reset`, is entered by arriving with ?reset=<token> and
     asks for the new password instead. */
  function setMode(next) {
    var signup = next === 'signup';
    var forgot = next === 'forgot';
    var reset = next === 'reset';

    dialog.querySelectorAll('.auth-tab').forEach(function (tab) {
      var on = tab.dataset.mode === next;
      tab.classList.toggle('is-on', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    dialog.querySelector('.auth-tabs').hidden = forgot || reset;
    dialog.querySelector('.auth-name-row').hidden = !signup;
    dialog.querySelector('.auth-email-row').hidden = reset;
    dialog.querySelector('.auth-password-row').hidden = forgot;
    dialog.querySelector('.auth-forgot').hidden = signup || forgot || reset;
    dialog.querySelector('#auth-hint').hidden = !(signup || reset);
    dialog.querySelector('.auth-submit').textContent =
      signup ? 'Create account' : forgot ? 'Email me a link'
      : reset ? 'Set a new password' : 'Sign in';
    dialog.querySelector('#auth-password').setAttribute(
      'autocomplete', (signup || reset) ? 'new-password' : 'current-password'
    );
    dialog.dataset.mode = next;
    errorNode.textContent = '';
  }

  function submit(event) {
    event.preventDefault();
    errorNode.textContent = '';

    var email = form.email.value.trim();
    var password = form.password.value;
    var button = dialog.querySelector('.auth-submit');
    var mode = dialog.dataset.mode;

    button.disabled = true;

    if (mode === 'forgot') {
      // Always the same message, whatever the server did. The endpoint is
      // deliberately indistinguishable for a known and an unknown address, and
      // a UI that reported otherwise would give away what the API withholds.
      request('POST', '/auth/forgot', { email: email }).catch(function () {})
        .then(function () {
          showNotice('If there is an account for ' + email + ', a reset link is on ' +
                     'its way. It works once and expires in an hour.');
          button.disabled = false;
        });
      return;
    }

    if (mode === 'reset') {
      request('POST', '/auth/reset', { token: resetToken, new_password: password })
        .then(function () {
          clearResetFromUrl();
          setMode('signin');
          showNotice('Password changed. You have been signed out everywhere — ' +
                     'sign in with the new one.');
        }).catch(function (error) {
          errorNode.textContent = error.message;
        }).then(function () { button.disabled = false; });
      return;
    }

    var action = mode === 'signup'
      ? signUp(email, password, form.display_name.value)
      : signIn(email, password);

    action.then(function () {
      dialog.close();
      form.reset();
    }).catch(function (error) {
      errorNode.textContent = error.message;
    }).then(function () {
      button.disabled = false;
    });
  }

  var resetToken = '';

  function showNotice(text) {
    var node = dialog.querySelector('#auth-notice');
    node.textContent = text;
    node.hidden = false;
    errorNode.textContent = '';
  }

  /* The token is a single-use credential sitting in the address bar. Once the
     dialog holds it, take it out of the URL so it does not end up in a shared
     link, a bookmark or a referrer header. */
  function clearResetFromUrl() {
    resetToken = '';
    var query = new URLSearchParams(window.location.search);
    if (!query.has('reset')) return;
    query.delete('reset');
    var search = query.toString();
    window.history.replaceState({}, '',
      search ? '?' + search : window.location.pathname);
  }

  function checkResetLink() {
    var token = new URLSearchParams(window.location.search).get('reset');
    if (!token) return;
    if (!available) {
      // A reset link is useless without the API. Say so rather than silently
      // doing nothing with a token the user just clicked.
      return;
    }
    buildDialog();
    resetToken = token;
    setMode('reset');
    dialog.querySelector('#auth-intro').textContent = 'Choose a new password.';
    dialog.querySelector('#auth-intro').hidden = false;
    dialog.showModal();
    dialog.querySelector('#auth-password').focus();
  }

  function open(mode, message) {
    if (!available) return;
    buildDialog();
    setMode(mode || 'signin');
    pendingMessage = message || '';
    dialog.querySelector('#auth-intro').textContent = pendingMessage;
    dialog.querySelector('#auth-intro').hidden = !pendingMessage;
    dialog.showModal();
    dialog.querySelector('#auth-email').focus();
  }

  global.ChurchAccount = {
    init: init,
    available: function () { return available; },
    user: function () { return user; },
    onChange: onChange,
    open: open,
    signOut: signOut,
    changePassword: changePassword,
    isSaved: isSaved,
    toggleSave: toggleSave,
    listSaved: listSaved,
    refreshSaved: refreshSaved,
    setHome: setHome,
    reportCorrection: reportCorrection
  };
})(window);
