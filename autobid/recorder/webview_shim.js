/*
 * Эмуляция Telegram WebView для записи мини-приложений в обычном браузере (record_webapp.py).
 *
 * 1) Во всех фреймах: записывает клики, ввод в поля и Enter → window.__autobidRec(kind, data).
 * 2) В главном окне: подставляет window.TelegramWebviewProxy — так Telegram на Android принимает вызовы
 *    приложения (postEvent) — и отвечает событиями через window.Telegram.WebView.receiveEvent, как
 *    Android-клиент. Рисует поверх страницы MainButton/SecondaryButton, BackButton, SettingsButton,
 *    всплывающие окна (showPopup/showConfirm/showAlert) и подсказки.
 *    CloudStorage (invokeCustomMethod) уходит в настоящий Telegram через window.__autobidInvoke.
 *
 * Список методов и событий — по @telegram-apps/bridge 2.11 и официальному telegram-web-app.js.
 * __AUTOBID_CFG__ подставляет Python: {platform, version, theme, label}.
 */
(function () {
  'use strict';
  var CFG = __AUTOBID_CFG__;
  if (window.__autobidShimInstalled) return;
  window.__autobidShimInstalled = true;

  var isTop = false;
  try { isTop = window.top === window; } catch (e) { isTop = false; }

  // ------------------------------------------------------------------ передача событий в рекордер
  var queue = [];
  var host = null;   // наш интерфейс (только в главном окне)
  var root = null;

  function flush() {
    if (typeof window.__autobidRec !== 'function') {
      setTimeout(flush, 100);
      return;
    }
    while (queue.length) {
      var item = queue.shift();
      try {
        var r = window.__autobidRec(item[0], item[1]);
        if (r && typeof r.then === 'function') r.then(null, function () {});
      } catch (e) { /* страница закрывается */ }
    }
  }

  function rec(kind, data) {
    queue.push([kind, data || {}]);
    if (queue.length === 1) flush();
  }

  // ------------------------------------------------------------------ описание элементов
  var CLICKABLE = 'button, a, input, select, textarea, label, summary, [role="button"], [role="tab"], ' +
    '[role="link"], [role="menuitem"], [role="option"], [role="checkbox"], [role="switch"], [role="radio"], [onclick]';

  function clip(s, n) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, n || 120); }

  function attr(el, name) { try { return el.getAttribute ? el.getAttribute(name) : null; } catch (e) { return null; } }

  function textOf(el) {
    var t = '';
    try { t = el.innerText || el.textContent || ''; } catch (e) { t = ''; }
    t = clip(t);
    if (!t) t = clip(attr(el, 'aria-label') || attr(el, 'title') || attr(el, 'alt') || attr(el, 'placeholder') || '');
    if (!t && el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type || '')) t = clip(el.value);
    return t;
  }

  function cssPath(el) {
    var parts = [];
    for (var n = 0; el && el.nodeType === 1 && n < 5; n++) {
      var s = el.tagName.toLowerCase();
      if (el.id) { parts.unshift(s + '#' + el.id); break; }
      var cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      var parent = el.parentElement;
      if (parent) {
        var same = Array.prototype.filter.call(parent.children, function (c) { return c.tagName === el.tagName; });
        if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      }
      parts.unshift(s);
      el = parent;
    }
    return parts.join(' > ');
  }

  function describe(el) {
    var d = { tag: el.tagName ? el.tagName.toLowerCase() : '', text: textOf(el) };
    var names = { role: 'role', aria: 'aria-label', name: 'name', type: 'type', href: 'href' };
    for (var k in names) { var v = attr(el, names[k]); if (v) d[k] = clip(v, 200); }
    var testid = attr(el, 'data-testid') || attr(el, 'data-test') || attr(el, 'data-qa') || attr(el, 'data-test-id') || attr(el, 'data-cy');
    if (testid) d.testid = clip(testid, 120);
    if (el.id) d.id = clip(el.id, 120);
    if (typeof el.className === 'string' && el.className) d.cls = clip(el.className, 120);
    d.path = cssPath(el);
    return d;
  }

  function isOurs(e) {
    if (!host) return false;
    var path = e.composedPath ? e.composedPath() : [];
    return path.indexOf(host) !== -1;
  }

  // ------------------------------------------------------------------ клики и ввод (во всех фреймах)
  function pointerAncestor(el, levels) {
    // в React/Vue кнопки часто — просто div с cursor:pointer и обработчиком на корне приложения
    for (var n = el, i = 0; n && n.nodeType === 1 && i <= levels; n = n.parentElement, i++) {
      try { if (getComputedStyle(n).cursor === 'pointer') return n; } catch (e) { return null; }
    }
    return null;
  }

  function around(el, text) {
    // текст ближайшего предка, который длиннее текста элемента — подпись рядом («Купить» у «TON»)
    for (var n = el.parentElement, i = 0; n && n.nodeType === 1 && i < 3; n = n.parentElement, i++) {
      var t = textOf(n);
      if (t && t !== text) return t.length <= 60 ? t : '';
    }
    return '';
  }

  document.addEventListener('click', function (e) {
    if (isOurs(e)) return;
    var el = e.target;
    if (el && el.nodeType !== 1) el = el.parentElement;
    if (!el) return;
    var matched = (el.closest && el.closest(CLICKABLE)) || pointerAncestor(el, 4);
    var c = matched || el;
    var d = describe(c);
    if (c !== el) {
      var inner = textOf(el);
      if (inner && inner !== d.text) d.inner = inner;
    }
    if (!matched || d.text.length < 4) {   // непонятно, что за элемент, — добавим подпись рядом
      var ctx = around(c, d.text);
      if (ctx) d.around = ctx;
    }
    d.x = Math.round(e.clientX);
    d.y = Math.round(e.clientY);
    rec('click', d);
  }, true);

  var timers = typeof WeakMap === 'function' ? new WeakMap() : null;
  var lastValue = typeof WeakMap === 'function' ? new WeakMap() : null;

  function sensitive(el) {
    var s = [el.type, el.name, el.id, el.autocomplete, attr(el, 'aria-label'), el.placeholder].join(' ').toLowerCase();
    return el.type === 'password' || /pass|парол|pin|otp|one-time|2fa|secret|seed|mnemonic|cvv|cvc|код|code/.test(s);
  }

  function labelOf(el) {
    var l = '';
    try { if (el.labels && el.labels.length) l = el.labels[0].innerText; } catch (e) { l = ''; }
    return clip(l || attr(el, 'aria-label') || el.placeholder || el.name || el.id || '', 80);
  }

  function recordInput(el) {
    if (!el || !('value' in el) || el.tagName === 'BUTTON') return;
    var value = sensitive(el) ? '***' : String(el.value == null ? '' : el.value).slice(0, 300);
    var key = value + '|' + (el.checked ? 1 : 0);
    if (lastValue && lastValue.get(el) === key) return;
    if (lastValue) lastValue.set(el, key);
    var d = describe(el);
    delete d.text;
    d.label = labelOf(el);
    d.value = value;
    if (el.type === 'checkbox' || el.type === 'radio') d.checked = !!el.checked;
    rec('input', d);
  }

  document.addEventListener('input', function (e) {
    var el = e.target;
    if (!el || isOurs(e) || !timers) return;
    clearTimeout(timers.get(el));
    timers.set(el, setTimeout(function () { recordInput(el); }, 700));
  }, true);

  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || isOurs(e)) return;
    if (timers) clearTimeout(timers.get(el));
    recordInput(el);
  }, true);

  document.addEventListener('keydown', function (e) {
    var el = e.target;
    if (e.key !== 'Enter' || !el || isOurs(e) || !/^(INPUT|TEXTAREA)$/.test(el.tagName || '')) return;
    if (timers) clearTimeout(timers.get(el));
    recordInput(el);
    rec('key', { key: 'Enter', label: labelOf(el) });
  }, true);

  document.addEventListener('submit', function (e) {
    if (e.target && e.target.nodeType === 1) rec('submit', describe(e.target));
  }, true);

  if (!isTop) return;

  // ------------------------------------------------------------------ эмуляция Telegram (главное окно)
  var BAR_H = 64;
  var state = { main: {}, secondary: {}, back: false, settings: false, popup: null };
  var ui = {};
  var memStore = {};   // эмулированные Device/SecureStorage держим в памяти, а не в localStorage (не оседают в профиле)
  // Нажатия и ответы на вопросы: если приложение ещё не подписалось — теряем, как настоящий Android,
  // а не доставляем через 2 секунды уже в другой экран (иначе одно нажатие срабатывает дважды).
  var INPUT_EVENTS = { main_button_pressed: 1, secondary_button_pressed: 1, back_button_pressed: 1,
    settings_button_pressed: 1, popup_closed: 1, invoice_closed: 1, scan_qr_popup_closed: 1, qr_text_received: 1 };

  function receiver() {
    return (window.Telegram && window.Telegram.WebView && window.Telegram.WebView.receiveEvent) ||
      (window.TelegramGameProxy && window.TelegramGameProxy.receiveEvent) || window.TelegramGameProxy_receiveEvent;
  }

  function deliver(type, data, attempt) {
    // Ответы Telegram всегда асинхронны — приложение успевает подписаться на событие.
    // Если SDK ещё грузится (resize/тема до загрузки скриптов) — пробуем ещё 5 секунд, но только для
    // событий-состояний; нажатия и ответы на вопросы не копим, иначе они «выстрелят» в поздний обработчик.
    setTimeout(function () {
      var f = receiver();
      if (typeof f !== 'function') {
        if (INPUT_EVENTS[type]) { rec('shim_note', { dropped: type }); return; }
        if ((attempt || 0) < 50) deliver(type, data, (attempt || 0) + 1);
        else if (location.protocol !== 'about:') rec('shim_error', { where: 'deliver ' + type, error: 'у страницы нет SDK Telegram' });
        return;
      }
      try { f(type, data === undefined ? null : data); } catch (e) { rec('shim_error', { where: 'deliver ' + type, error: String(e) }); }
    }, attempt ? 100 : 0);
  }

  function barVisible() { return !!(state.main.is_visible || state.secondary.is_visible); }

  function barHeight() {
    if (!barVisible()) return 0;
    try { return Math.ceil(ui.bar.getBoundingClientRect().height) || BAR_H; } catch (e) { return BAR_H; }
  }

  function viewport() {
    // реальная высота панели (при MainButton+SecondaryButton она выше 64), иначе 53px контента прячется под ней
    return { height: Math.max(100, window.innerHeight - barHeight()), width: window.innerWidth,
      is_expanded: true, is_state_stable: true };
  }

  function sendViewport() { deliver('viewport_changed', viewport()); }

  var CSS = [
    ':host{all:initial}',
    '*{box-sizing:border-box;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif}',
    '.bar{position:fixed;left:0;right:0;bottom:0;height:' + BAR_H + 'px;display:none;gap:8px;padding:8px 12px;',
    'background:#1d2733;border-top:1px solid #2c3a49;pointer-events:auto}',
    '.bar.on{display:flex}',
    '.bar.col{flex-direction:column;height:auto}',
    '.tg-btn{flex:1;min-height:46px;border:0;border-radius:10px;font-weight:600;font-size:15px;cursor:pointer;',
    'color:#fff;background:#50a8eb}',
    '.tg-btn[disabled]{opacity:.5;cursor:default}',
    '.tg-btn.second{background:transparent;color:#64b5ef;border:1px solid #3a4a5c}',
    '.tg-btn.hidden{display:none}',
    '.tag{position:absolute;top:-17px;left:8px;font-size:10px;color:#9fb3c8;background:#1d2733;padding:1px 6px;',
    'border-radius:6px 6px 0 0;pointer-events:none}',
    '.pill{position:fixed;top:8px;display:none;pointer-events:auto;background:#1d2733e6;color:#fff;',
    'border:1px solid #3a4a5c;border-radius:16px;padding:6px 12px;font-size:14px;cursor:pointer}',
    '.pill.on{display:block}',
    '.back{left:8px}.settings{right:8px}',
    '.badge{position:fixed;top:3px;left:50%;transform:translateX(-50%);font-size:10px;color:#fff;',
    'background:#c62828cc;padding:1px 7px;border-radius:8px;pointer-events:none;opacity:.8}',
    '.toasts{position:fixed;left:50%;bottom:' + (BAR_H + 12) + 'px;transform:translateX(-50%);display:flex;',
    'flex-direction:column;gap:6px;align-items:center;pointer-events:none;width:92%}',
    '.toast{background:#000000d0;color:#fff;padding:8px 12px;border-radius:8px;font-size:13px;max-width:380px}',
    '.wrap{position:fixed;inset:0;background:#00000080;display:flex;align-items:center;justify-content:center;',
    'pointer-events:auto}',
    '.popup{background:#212d3b;color:#fff;border-radius:12px;padding:16px;width:300px;font-size:14px;',
    'box-shadow:0 8px 30px #00000080}',
    '.ptitle{font-weight:700;margin-bottom:8px}.pmsg{white-space:pre-wrap;margin-bottom:14px}',
    '.pbtns{display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap}',
    '.pbtn{background:transparent;color:#64b5ef;border:0;font-weight:600;cursor:pointer;padding:6px 8px;font-size:14px}',
    '.pbtn.destructive{color:#ee686f}'
  ].join('');

  function make(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  function noFocus(btn) { btn.tabIndex = -1; return btn; }   // не держим фокус: иначе Enter/Пробел жмут кнопку повторно

  function ensureUI() {
    if (host && host.isConnected) { rehome(); return true; }
    var parent = document.documentElement;
    if (!parent) return false;
    host = document.createElement('autobid-tg-ui');
    host.style.cssText = 'all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
    root = host.attachShadow({ mode: 'open' });
    root.appendChild(make('style', null, CSS));
    ui.badge = make('div', 'badge', '● запись');
    ui.bar = make('div', 'bar');
    ui.tag = make('div', 'tag', CFG.label || 'Telegram (эмуляция)');
    ui.main = noFocus(make('button', 'tg-btn'));
    ui.second = noFocus(make('button', 'tg-btn second'));
    ui.bar.appendChild(ui.tag);
    ui.bar.appendChild(ui.second);
    ui.bar.appendChild(ui.main);
    ui.back = noFocus(make('button', 'pill back', '‹ Назад'));
    ui.settings = noFocus(make('button', 'pill settings', '⚙'));
    ui.toasts = make('div', 'toasts');
    [ui.badge, ui.bar, ui.back, ui.settings, ui.toasts].forEach(function (x) { root.appendChild(x); });

    ui.main.__act = function () {
      if (state.main.is_active === false) return;
      rec('tg_ui', { what: 'main_button', text: state.main.text || '' });
      deliver('main_button_pressed', null);
    };
    ui.second.__act = function () {
      if (state.secondary.is_active === false) return;
      rec('tg_ui', { what: 'secondary_button', text: state.secondary.text || '' });
      deliver('secondary_button_pressed', null);
    };
    ui.back.__act = function () {
      rec('tg_ui', { what: 'back_button', text: 'Назад' });
      deliver('back_button_pressed', null);
    };
    ui.settings.__act = function () {
      rec('tg_ui', { what: 'settings_button', text: 'Настройки' });
      deliver('settings_button_pressed', null);
    };
    parent.appendChild(host);
    rehome();
    return true;
  }

  function topModal() {
    // верхний открытый модальный <dialog>: в нём всё вне диалога становится inert, в т.ч. наша панель
    try {
      var l = document.querySelectorAll('dialog[open]');
      for (var i = l.length - 1; i >= 0; i--) { if (l[i].matches && l[i].matches(':modal')) return l[i]; }
    } catch (e) { /* старый браузер без :modal */ }
    return null;
  }

  function rehome() {
    if (!host) return;
    var target = topModal() || document.documentElement;
    if (host.parentNode === target) return;
    try {
      if (host.__popover) { try { host.hidePopover(); } catch (e) { /* */ } host.removeAttribute('popover'); host.__popover = false; }
      target.appendChild(host);
      if (target !== document.documentElement && 'showPopover' in host) {
        // popover выносит панель в top layer поверх диалога и держит её координаты относительно окна
        host.setAttribute('popover', 'manual'); host.__popover = true;
        try { host.showPopover(); } catch (e) { /* */ }
      }
    } catch (e) { /* */ }
  }

  function styleButton(btn, st) {
    var visible = !!st.is_visible;
    btn.classList.toggle('hidden', !visible);
    btn.textContent = (st.text || '') + (st.is_progress_visible ? ' …' : '');
    btn.disabled = st.is_active === false;
    if (st.color) btn.style.background = st.color;
    if (st.text_color) btn.style.color = st.text_color;
  }

  function render() {
    if (!ensureUI()) return;
    styleButton(ui.main, state.main);
    styleButton(ui.second, state.secondary);
    var pos = state.secondary.position || 'left';
    ui.bar.classList.toggle('col', pos === 'top' || pos === 'bottom');
    ui.bar.style.flexDirection = pos === 'top' ? 'column' : pos === 'bottom' ? 'column-reverse' : pos === 'right' ? 'row-reverse' : 'row';
    ui.bar.classList.toggle('on', barVisible());
    ui.back.classList.toggle('on', !!state.back);
    ui.settings.classList.toggle('on', !!state.settings);
  }

  function toast(text) {
    if (!ensureUI()) return;
    var t = make('div', 'toast', text);
    ui.toasts.appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 4500);
  }
  window.__autobidToast = toast;

  function closePopup(result) {
    if (!state.popup) return;
    if (state.popup.parentNode) state.popup.parentNode.removeChild(state.popup);
    state.popup = null;
    if (result) deliver('popup_closed', result);
  }

  function openPopup(p) {
    if (!ensureUI()) return;
    closePopup(null);
    var wrap = make('div', 'wrap');
    var card = make('div', 'popup');
    if (p.title) card.appendChild(make('div', 'ptitle', p.title));
    card.appendChild(make('div', 'pmsg', p.message || ''));
    var row = make('div', 'pbtns');
    var list = p.buttons && p.buttons.length ? p.buttons : [{ id: '', type: 'close' }];
    list.forEach(function (b) {
      var label = b.text || { ok: 'OK', close: 'Закрыть', cancel: 'Отмена' }[b.type] || 'OK';
      var btn = noFocus(make('button', 'pbtn' + (b.type === 'destructive' ? ' destructive' : ''), label));
      btn.__act = function () {
        rec('tg_ui', { what: 'popup', button_id: b.id, text: label, title: p.title || '' });
        closePopup({ button_id: b.id });
      };
      row.appendChild(btn);
    });
    card.appendChild(row);
    wrap.appendChild(card);
    wrap.__act = function () {   // клик по затемнению (не по карточке) — закрытие без выбора
      rec('tg_ui', { what: 'popup', button_id: null, text: '(закрыто без выбора)', title: p.title || '' });
      closePopup({});
    };
    wrap.__wrapOnly = true;
    root.appendChild(wrap);
    state.popup = wrap;
  }

  function setButton(which, p) {
    var st = state[which];
    for (var k in (p || {})) st[k] = p[k];
    render();
    sendViewport();
  }

  function invokeCustom(p) {
    var call = window.__autobidInvoke;
    if (typeof call !== 'function') {
      deliver('custom_method_invoked', { req_id: p.req_id, error: 'UNSUPPORTED' });
      return;
    }
    Promise.resolve(call('custom_method', { method: p.method, params: p.params })).then(function (r) {
      var out = { req_id: p.req_id };
      if (r && r.error != null) out.error = r.error; else out.result = r ? r.result : null;
      deliver('custom_method_invoked', out);
    }, function (e) {
      deliver('custom_method_invoked', { req_id: p.req_id, error: String(e) });
    });
  }

  function storage(method, p) {
    p = p || {};
    var secure = method.indexOf('secure') !== -1;
    var prefix = secure ? 'ss:' : 'ds:';          // храним в памяти (memStore), не в localStorage профиля
    var ev = secure ? 'secure_storage_' : 'device_storage_';
    var key = prefix + (p.key || '');
    if (/_save_key$/.test(method)) {
      if (p.value === null || p.value === undefined) delete memStore[key];
      else memStore[key] = String(p.value);
      deliver(ev + 'key_saved', { req_id: p.req_id });
    } else if (/_get_key$/.test(method)) {
      var out = { req_id: p.req_id, value: key in memStore ? memStore[key] : null };
      if (secure) out.can_restore = false;
      deliver(ev + 'key_received', out);
    } else if (/_clear$/.test(method)) {
      Object.keys(memStore).forEach(function (k) { if (k.indexOf(prefix) === 0) delete memStore[k]; });
      deliver(ev + 'cleared', { req_id: p.req_id });
    } else {
      deliver(ev + 'failed', { req_id: p.req_id, error: 'UNSUPPORTED' });
    }
  }

  // Методы, которые не пишем в журнал (частые и ничего не говорят о работе приложения)
  var QUIET = { web_app_trigger_haptic_feedback: 1, iframe_ready: 1, iframe_will_reload: 1, web_app_request_viewport: 1,
    web_app_request_theme: 1, web_app_request_safe_area: 1, web_app_request_content_safe_area: 1,
    web_app_set_header_color: 1, web_app_set_background_color: 1, web_app_set_bottom_bar_color: 1,
    web_app_setup_swipe_behavior: 1, web_app_setup_closing_behavior: 1, web_app_invoke_custom_method: 1 };

  var NO_SENSORS = { web_app_start_accelerometer: 'accelerometer_failed', web_app_start_gyroscope: 'gyroscope_failed',
    web_app_start_device_orientation: 'device_orientation_failed' };
  var SENSOR_STOP = { web_app_stop_accelerometer: 'accelerometer_stopped', web_app_stop_gyroscope: 'gyroscope_stopped',
    web_app_stop_device_orientation: 'device_orientation_stopped' };

  function handle(method, p) {
    if (!QUIET[method]) rec('tg', { method: method, params: p === undefined ? null : p });
    if (NO_SENSORS[method]) { deliver(NO_SENSORS[method], { error: 'UNSUPPORTED' }); return; }
    if (SENSOR_STOP[method]) { deliver(SENSOR_STOP[method], null); return; }
    if (/^web_app_(device|secure)_storage_/.test(method)) { storage(method, p); return; }
    switch (method) {
      case 'web_app_request_theme': deliver('theme_changed', { theme_params: CFG.theme }); break;
      case 'web_app_request_viewport': case 'web_app_expand': sendViewport(); break;
      case 'web_app_request_safe_area': deliver('safe_area_changed', { top: 0, bottom: 0, left: 0, right: 0 }); break;
      case 'web_app_request_content_safe_area':
        deliver('content_safe_area_changed', { top: 0, bottom: 0, left: 0, right: 0 }); break;
      case 'web_app_setup_main_button': setButton('main', p); break;
      case 'web_app_setup_secondary_button': setButton('secondary', p); break;
      case 'web_app_setup_back_button': state.back = !!(p && p.is_visible); render(); break;
      case 'web_app_setup_settings_button': state.settings = !!(p && p.is_visible); render(); break;
      case 'web_app_open_popup': openPopup(p || {}); break;
      case 'web_app_open_link':
        if (p && p.url) { toast('Ссылка открыта в новой вкладке'); try { window.open(p.url, '_blank'); } catch (e) { /* */ } }
        break;
      case 'web_app_open_tg_link': toast('Ссылка Telegram (не открывается в браузере): t.me' + ((p && p.path_full) || '')); break;
      case 'web_app_open_invoice':
        deliver('invoice_closed', { slug: (p && p.slug) || '', status: 'cancelled' });
        toast('Оплата счёта не эмулируется'); break;
      case 'web_app_close': toast('Приложение попросило закрыться. Вкладку можно закрыть.'); break;
      case 'web_app_data_send': toast('sendData не отправлен в Telegram — это только запись'); break;
      case 'web_app_switch_inline_query': toast('switchInlineQuery не эмулируется'); break;
      case 'web_app_invoke_custom_method': invokeCustom(p || {}); break;
      case 'web_app_read_text_from_clipboard':
        deliver('clipboard_text_received', { req_id: p && p.req_id, data: null }); break;
      case 'web_app_request_phone': deliver('phone_requested', { status: 'cancelled' }); toast('Запрос телефона отклонён'); break;
      case 'web_app_request_write_access': deliver('write_access_requested', { status: 'allowed' }); break;
      case 'web_app_biometry_get_info': case 'web_app_biometry_request_access':
        deliver('biometry_info_received', { available: false }); break;
      case 'web_app_biometry_request_auth': deliver('biometry_auth_requested', { status: 'failed' }); break;
      case 'web_app_biometry_update_token': deliver('biometry_token_updated', { status: 'failed' }); break;
      case 'web_app_check_location': deliver('location_checked', { available: false }); break;
      case 'web_app_request_location': deliver('location_requested', { available: false }); break;
      case 'web_app_check_home_screen': deliver('home_screen_checked', { status: 'unsupported' }); break;
      case 'web_app_add_to_home_screen': deliver('home_screen_failed', null); break;
      case 'web_app_request_fullscreen': deliver('fullscreen_failed', { error: 'UNSUPPORTED' }); break;
      case 'web_app_exit_fullscreen': deliver('fullscreen_changed', { is_fullscreen: false }); break;
      case 'web_app_request_emoji_status_access': deliver('emoji_status_access_requested', { status: 'cancelled' }); break;
      case 'web_app_set_emoji_status': deliver('emoji_status_failed', { error: 'USER_DECLINED' }); break;
      case 'web_app_send_prepared_message': deliver('prepared_message_failed', { error: 'UNSUPPORTED' }); break;
      case 'web_app_request_file_download': deliver('file_download_requested', { status: 'cancelled' }); break;
      case 'web_app_open_scan_qr_popup': toast('Сканер QR не эмулируется'); deliver('scan_qr_popup_closed', null); break;
      case 'web_app_hide_keyboard': try { if (document.activeElement) document.activeElement.blur(); } catch (e) { /* */ } break;
      default: break;
    }
  }

  window.TelegramWebviewProxy = {
    postEvent: function (eventType, eventData) {
      var p = eventData;
      if (typeof eventData === 'string') {
        try { p = eventData === '' ? undefined : JSON.parse(eventData); } catch (e) { p = eventData; }
      }
      if (p === '') p = undefined;
      try { handle(String(eventType), p); } catch (e) { rec('shim_error', { where: String(eventType), error: String(e) }); }
    }
  };

  // Наши кнопки — DOM-узлы в документе приложения, поэтому их клики доходят до его обработчиков
  // (и закрывают его шторки/меню как «клик снаружи»), а фокус на кнопке ловит Enter/Пробел.
  // Экранируем свои события в фазе перехвата: до приложения они не доходят, а действие кнопки
  // выполняем сами. init-скрипт стоит раньше кода приложения, так что этот перехватчик — первый.
  function ownAction(e) {
    var path = e.composedPath ? e.composedPath() : [];
    for (var i = 0; i < path.length; i++) {
      if (path[i] === host) break;
      if (path[i] && path[i].__act) return path[i];
    }
    return null;
  }
  ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'auxclick', 'contextmenu',
    'touchstart', 'touchend', 'focusin', 'focusout'].forEach(function (type) {
    window.addEventListener(type, function (e) {
      if (!isOurs(e)) return;
      e.stopImmediatePropagation();
      if (type === 'mousedown') { try { e.preventDefault(); } catch (_) { /* */ } }   // фокус остаётся в поле приложения
      if (type === 'click') {
        var el = ownAction(e);
        if (el && el.__act && (!el.__wrapOnly || (e.composedPath && e.composedPath()[0] === el))) el.__act();
      }
    }, true);
  });

  window.addEventListener('resize', sendViewport);
  document.addEventListener('DOMContentLoaded', render);
  setInterval(function () { if (host && !host.isConnected) render(); }, 1000);
  try {
    new MutationObserver(rehome).observe(document, { subtree: true, attributes: true, attributeFilter: ['open'] });
  } catch (e) { /* без MutationObserver просто не переносим панель в модальный диалог */ }
})();
