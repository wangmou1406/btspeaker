/* ==========================================================================
 * 蓝音管家 BTSpeaker · 独立 Web 控制台
 * 原生 JavaScript，无框架、无构建、无第三方库、无外部网络资源。
 * 页面：总览 / 音箱 / 自动化 / 设置（hash 路由），每 3 秒轮询 /api/status。
 * ========================================================================== */
(function () {
  'use strict';

  /* ---------------------------------------------------------------- 常量 */

  var TOKEN_KEY = 'bts_token';
  var POLL_MS = 3000;
  var ROUTES = ['dashboard', 'devices', 'rules', 'settings'];
  var ROUTE_TITLES = { dashboard: '总览', devices: '音箱', rules: '自动化', settings: '设置' };
  var DAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  var DAY_LABELS = ['一', '二', '三', '四', '五', '六', '日'];
  var BT_EVENT_LABELS = { connected: '蓝牙连上', disconnected: '蓝牙断开', paired: '蓝牙配对' };
  var ACTION_TYPES = [
    { value: 'play', label: '播放声音' },
    { value: 'tts', label: '语音播报' },
    { value: 'ai', label: 'AI 播报' },
    { value: 'volume', label: '设置音量' },
    { value: 'connect', label: '连接音箱' },
    { value: 'disconnect', label: '断开音箱' },
    { value: 'http', label: 'HTTP 请求' },
    { value: 'wait', label: '等待' },
    { value: 'log', label: '写入日志' },
    { value: 'command', label: '执行命令' }
  ];
  var COND_TYPES = [
    { value: 'connected', label: '需要已连接' },
    { value: 'time_between', label: '时间段' },
    { value: 'weekday_in', label: '星期范围' }
  ];
  var SECTION_LABELS = { tts: 'TTS 设置', ai: 'AI 设置', player: '播放器设置', bluetooth: '蓝牙设置', security: '安全设置' };
  var TRIGGER_TYPES = [
    { value: 'schedule', label: '定时' },
    { value: 'bluetooth', label: '蓝牙事件' },
    { value: 'boot', label: '开机延迟' },
    { value: 'manual', label: '仅手动' }
  ];

  /* ---------------------------------------------------------------- 状态 */

  var S = {
    token: '',
    online: null,            // null 未知 / true 在线 / false 离线
    lastError: '',
    status: null,
    route: 'dashboard',
    parts: [],
    navSeq: 0,
    routeOffline: false,
    logs: [],
    events: [],
    library: [],
    libraryLoaded: false,
    rules: [],
    ruleCounts: { total: 0, enabled: 0 },
    rulesLoaded: false,
    settingsConfig: null,
    system: null,
    diag: null,
    deviceData: null,
    defaultDevice: '',
    scan: null,
    editorId: '',
    drafts: { ttsText: '', quickSound: '', devSound: '' }
  };

  var pollTimer = null;
  var pollBusy = false;
  var statusWaiters = [];
  var routeTimers = [];
  var scanTimer = null;
  var tokenModal = null;
  var rulesReloadTimer = null;
  var lastGlobalToast = 0;

  /* ---------------------------------------------------------------- 工具 */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function icon(name, cls) {
    var id = String(name == null ? '' : name);
    if (id.indexOf('i-') !== 0) id = 'i-' + id;
    return '<svg class="ic ' + (cls || '') + '" aria-hidden="true" focusable="false"><use href="#' + id + '"></use></svg>';
  }

  function escAttr(value) { return esc(value); }

  function toInt(value, fallback) {
    var n = parseInt(value, 10);
    return Number.isFinite(n) ? n : fallback;
  }

  function clampInt(value, min, max) {
    var n = Number(value);
    if (!Number.isFinite(n)) n = min;
    return Math.min(max, Math.max(min, Math.round(n)));
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function toTime(value) {
    if (!value) return 0;
    if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
    var str = String(value);
    var t = Date.parse(str);
    if (isNaN(t) && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(str)) {
      t = Date.parse(str.replace(' ', 'T'));
    }
    return isNaN(t) ? 0 : t;
  }

  function fmtClock(value) {
    if (!value) return '—';
    var str = String(value);
    if (/^\d{2}:\d{2}/.test(str)) return str.slice(0, 8);
    var t = toTime(value);
    if (!t) return str;
    var d = new Date(t);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function fmtDateTime(value) {
    var t = toTime(value);
    if (!t) return value ? String(value) : '—';
    var d = new Date(t);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function fmtAgo(value) {
    var t = toTime(value);
    if (!t) return '—';
    var diff = Math.floor((Date.now() - t) / 1000);
    if (diff < 0) return fmtIn(-diff);
    if (diff < 5) return '刚刚';
    if (diff < 60) return diff + ' 秒前';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + ' 天前';
    return fmtDateTime(value);
  }

  function fmtIn(sec) {
    var n = Math.max(0, Math.round(Number(sec) || 0));
    if (n < 60) return n + ' 秒后';
    if (n < 3600) {
      var m = Math.floor(n / 60);
      var s = n % 60;
      return m + ' 分钟' + (s ? ' ' + s + ' 秒' : '') + '后';
    }
    if (n < 86400) {
      var h = Math.floor(n / 3600);
      var mm = Math.floor((n % 3600) / 60);
      return h + ' 小时' + (mm ? ' ' + mm + ' 分钟' : '') + '后';
    }
    return Math.floor(n / 86400) + ' 天' + Math.floor((n % 86400) / 3600) + ' 小时后';
  }

  function fmtUptime(sec) {
    var n = Math.max(0, Math.round(Number(sec) || 0));
    if (!n) return '—';
    var d = Math.floor(n / 86400);
    var h = Math.floor((n % 86400) / 3600);
    var m = Math.floor((n % 3600) / 60);
    if (d) return d + ' 天 ' + h + ' 小时';
    if (h) return h + ' 小时 ' + m + ' 分钟';
    return m + ' 分钟';
  }

  function deepClone(value) {
    try { return JSON.parse(JSON.stringify(value)); } catch (e) { return value; }
  }

  function levelTone(level) {
    var lv = String(level || 'INFO').toUpperCase();
    if (lv === 'ERROR' || lv === 'CRITICAL') return 'danger';
    if (lv === 'WARNING' || lv === 'WARN') return 'warn';
    if (lv === 'DEBUG') return 'muted';
    return 'ok';
  }

  function deviceLabel(device) {
    var d = device || {};
    return d.name || d.address || '未命名设备';
  }

  function shortSource(source) {
    var s = String(source || '');
    if (!s) return '—';
    var parts = s.split('/');
    return parts[parts.length - 1] || s;
  }

  function setText(id, text) {
    var node = document.getElementById(id);
    if (node) node.textContent = text;
  }

  function setHTML(id, html) {
    var node = document.getElementById(id);
    if (node) node.innerHTML = html;
  }

  function patchHTML(node, html) {
    if (!node) return;
    if (node.dataset && node.dataset.sig === html) return;
    if (node.dataset) node.dataset.sig = html;
    node.innerHTML = html;
  }

  function patchById(id, html) { patchHTML(document.getElementById(id), html); }

  function patchIfIdle(id, html) {
    var node = document.getElementById(id);
    if (!node) return;
    var active = document.activeElement;
    if (active && node.contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) return;
    patchHTML(node, html);
  }

  function viewEl() { return document.getElementById('view'); }

  /* ---------------------------------------------------------------- API */

  function ApiError(message, code, status) {
    this.name = 'ApiError';
    this.message = message;
    this.code = code || 'error';
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function api(path, opts) {
    opts = opts || {};
    var method = opts.method || 'GET';
    var headers = {};
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timeout = opts.timeout || 20000;
    var timer = null;

    if (opts.body !== undefined) headers['Content-Type'] = 'application/json; charset=utf-8';
    if (S.token) headers['X-Api-Token'] = S.token;

    if (controller) timer = setTimeout(function () { controller.abort(); }, timeout);

    var init = {
      method: method,
      headers: headers,
      cache: 'no-store',
      credentials: 'same-origin'
    };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
    if (controller) init.signal = controller.signal;

    return fetch(path, init).then(function (res) {
      if (timer) clearTimeout(timer);
      return res.text().then(function (text) {
        var json = null;
        if (text) {
          try { json = JSON.parse(text); } catch (e) { json = null; }
        }
        if (res.status === 401) {
          markOnline(true);
          if (!opts.quiet) openTokenModal();
          throw new ApiError((json && json.error && json.error.message) || '需要访问令牌', 'unauthorized', 401);
        }
        if (!json) {
          markOnline(true);
          throw new ApiError(res.ok ? '服务返回了无法解析的数据' : '请求失败（HTTP ' + res.status + '）', 'bad_response', res.status);
        }
        if (!res.ok || json.ok === false) {
          markOnline(true);
          var errObj = json.error || {};
          throw new ApiError(errObj.message || ('请求失败（HTTP ' + res.status + '）'), errObj.code || ('http_' + res.status), res.status);
        }
        markOnline(true);
        return json.data;
      });
    }).catch(function (err) {
      if (timer) clearTimeout(timer);
      if (err instanceof ApiError) throw err;
      if (err && err.name === 'AbortError') {
        markOnline(false, '请求超时：后端服务没有响应');
        throw new ApiError('请求超时，请稍后重试', 'timeout', 0);
      }
      markOnline(false, '无法连接后端服务');
      throw new ApiError('无法连接后端服务，请确认 btspeaker 已经启动', 'network_error', 0);
    });
  }

  // 原始字节上传（非 multipart）：用于声音库文件保存，沿用统一信封与鉴权。
  function apiUpload(path, file, opts) {
    opts = opts || {};
    var headers = { 'Content-Type': file.type || 'application/octet-stream' };
    if (S.token) headers['X-Api-Token'] = S.token;
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = null;
    if (controller) timer = setTimeout(function () { controller.abort(); }, opts.timeout || 120000);
    var init = {
      method: 'POST',
      headers: headers,
      body: file,
      cache: 'no-store',
      credentials: 'same-origin'
    };
    if (controller) init.signal = controller.signal;
    return fetch(path, init).then(function (res) {
      if (timer) clearTimeout(timer);
      return res.text().then(function (text) {
        var json = null;
        if (text) {
          try { json = JSON.parse(text); } catch (e) { json = null; }
        }
        if (res.status === 401) {
          markOnline(true);
          openTokenModal();
          throw new ApiError('需要访问令牌', 'unauthorized', 401);
        }
        if (!json) {
          markOnline(true);
          throw new ApiError(res.ok ? '服务返回了无法解析的数据' : '请求失败（HTTP ' + res.status + '）', 'bad_response', res.status);
        }
        if (!res.ok || json.ok === false) {
          markOnline(true);
          var errObj = json.error || {};
          throw new ApiError(errObj.message || ('请求失败（HTTP ' + res.status + '）'), errObj.code || ('http_' + res.status), res.status);
        }
        markOnline(true);
        return json.data;
      });
    }).catch(function (err) {
      if (timer) clearTimeout(timer);
      if (err instanceof ApiError) throw err;
      if (err && err.name === 'AbortError') {
        markOnline(false, '上传超时：后端服务没有响应');
        throw new ApiError('上传超时，请稍后重试', 'timeout', 0);
      }
      markOnline(false, '无法连接后端服务');
      throw new ApiError('无法连接后端服务，请确认 btspeaker 已经启动', 'network_error', 0);
    });
  }

  /* ---------------------------------------------------------------- 连接状态与轮询 */

  function setDot(node, tone, pulse) {
    if (!node) return;
    node.className = 'dot' + (tone ? ' ' + tone : '') + (pulse ? ' pulse' : '');
  }

  function updateConnUI() {
    var online = S.online;
    var text = online === true ? '在线' : online === false ? '离线' : '连接中…';
    var tone = online === true ? 'ok' : online === false ? 'off' : 'warn';
    var pulse = online !== false;
    setDot(document.getElementById('side-dot'), tone, pulse);
    setDot(document.getElementById('top-dot'), tone, pulse);
    setText('side-conn-text', text);
    setText('top-conn-text', text);
  }

  function updateBanner() {
    var banner = document.getElementById('banner');
    if (!banner) return;
    if (S.online === false) {
      banner.hidden = false;
      setText('banner-title', '无法连接后端服务');
      var detail = (S.lastError && S.lastError.indexOf('无法连接后端服务') < 0) ? S.lastError + '。' : '';
      setText('banner-text', detail + '请确认 btspeaker 服务已启动，并且本机与路由器处于同一网络，然后点击重试。');
    } else {
      banner.hidden = true;
    }
  }

  function markOnline(online, message) {
    var changed = S.online !== online;
    S.online = online;
    if (!online) S.lastError = message || '无法连接后端服务';
    if (changed || !online) {
      updateConnUI();
      updateBanner();
    }
  }

  function applyGlobalStatus(st) {
    if (!st) return;
    var ver = st.version || '1.0.0';
    setText('side-version', 'v' + ver);
    setText('foot-version', 'v' + ver);
    var sim = document.getElementById('side-sim');
    if (sim) sim.hidden = !st.sim;
  }

  function ensureStatus(timeout) {
    if (S.status) return Promise.resolve(S.status);
    return new Promise(function (resolve) {
      statusWaiters.push(resolve);
      setTimeout(function () { resolve(S.status); }, timeout || 4500);
    });
  }

  function pollStatus() {
    // 返回 Promise：afterDeviceChange() 等调用方会 .then() 链式等待状态刷新，
    // 早期版本这里没有返回值，会导致前端报 "Cannot read properties of undefined (reading 'then')"。
    if (pollBusy) return Promise.resolve(S.status);
    pollBusy = true;
    return api('/api/status', { timeout: 9000 }).then(function (st) {
      if (!st) throw new ApiError('状态接口返回为空', 'bad_response', 0);
      S.status = st;
      markOnline(true);
      S.lastError = '';
      applyGlobalStatus(st);
      var waiters = statusWaiters;
      statusWaiters = [];
      waiters.forEach(function (fn) { try { fn(st); } catch (e) { /* 忽略 */ } });
      var def = routeDefs[S.route];
      if (def && typeof def.sync === 'function') {
        try { def.sync(st); } catch (e) { console.error('[sync]', e); }
      }
      if (S.routeOffline) {
        S.routeOffline = false;
        handleRoute();
      }
    }).catch(function (err) {
      if (err && err.code === 'unauthorized') {
        markOnline(true);
      } else {
        markOnline(false, err && err.message ? err.message : '无法连接后端服务');
      }
      var waiters = statusWaiters;
      statusWaiters = [];
      waiters.forEach(function (fn) { try { fn(null); } catch (e) { /* 忽略 */ } });
    }).then(function () {
      pollBusy = false;
    });
  }

  function startPolling() {
    stopPolling();
    pollStatus();
    pollTimer = setInterval(pollStatus, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function clearRouteTimers() {
    routeTimers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
    routeTimers = [];
  }

  function stopScanPolling() {
    if (scanTimer) {
      clearInterval(scanTimer);
      scanTimer = null;
    }
  }

  /* ---------------------------------------------------------------- Toast */

  function toast(message, type, detail) {
    var root = document.getElementById('toasts');
    if (!root) return;
    var kind = type || 'success';
    var name = kind === 'success' ? 'i-check' : kind === 'danger' ? 'i-alert' : kind === 'warn' ? 'i-alert' : 'i-info';
    var node = document.createElement('div');
    node.className = 'toast toast-' + kind;
    var now = new Date();
    var stamp = pad2(now.getHours()) + ':' + pad2(now.getMinutes()) + ':' + pad2(now.getSeconds());
    node.innerHTML = '<span class="toast-ic">' + icon(name) + '</span>' +
      '<div class="toast-body"><p>' + esc(message) + '</p>' +
      (detail ? '<span>' + esc(detail) + '</span>' : '') + '</div>' +
      '<span class="toast-time">' + stamp + '</span>' +
      '<button class="toast-x" type="button" aria-label="关闭提示">' + icon('i-x') + '</button>';
    var removed = false;
    function remove() {
      if (removed) return;
      removed = true;
      node.classList.add('is-out');
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 220);
    }
    node.querySelector('.toast-x').addEventListener('click', remove);
    root.appendChild(node);
    while (root.children.length > 4) root.removeChild(root.firstChild);
    setTimeout(remove, kind === 'danger' ? 5200 : 3600);
  }

  /* ---------------------------------------------------------------- 模态框 */

  function openModal(html, opts) {
    opts = opts || {};
    var root = document.getElementById('modal-root');
    if (!root) return null;
    var previousFocus = document.activeElement;
    root.innerHTML = '<div class="modal-backdrop"><div class="modal" role="dialog" aria-modal="true" aria-label="' +
      esc(opts.label || '对话框') + '">' + html + '</div></div>';
    var backdrop = root.firstElementChild;
    var modal = backdrop.firstElementChild;
    var closed = false;

    function close() {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKey);
      root.innerHTML = '';
      if (previousFocus && typeof previousFocus.focus === 'function') {
        try { previousFocus.focus(); } catch (e) { /* 忽略 */ }
      }
      if (typeof opts.onClose === 'function') opts.onClose();
    }

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    }

    document.addEventListener('keydown', onKey);
    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop && opts.dismissible !== false) close();
    });
    setTimeout(function () {
      var target = modal.querySelector('input:not([type="hidden"]), textarea, select') || modal.querySelector('button');
      if (target && typeof target.focus === 'function') target.focus();
    }, 40);
    return { root: root, backdrop: backdrop, modal: modal, close: close };
  }

  function confirmDialog(opts) {
    return new Promise(function (resolve) {
      var settled = false;
      function done(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      var dialog = openModal(
        '<div class="modal-head"><span class="modal-ic">' + icon('i-alert') + '</span><h2 class="modal-title">' +
        esc(opts.title || '请确认') + '</h2></div>' +
        '<p class="modal-text">' + esc(opts.message || '') + '</p>' +
        '<div class="modal-actions">' +
        '<button class="btn" type="button" data-role="cancel">' + esc(opts.cancelText || '取消') + '</button>' +
        '<button class="btn ' + (opts.danger ? 'btn-danger' : 'btn-primary') + '" type="button" data-role="ok">' +
        esc(opts.confirmText || '确认') + '</button></div>',
        { label: opts.title || '请确认', onClose: function () { done(false); } }
      );
      if (!dialog) { done(false); return; }
      dialog.modal.querySelector('[data-role="cancel"]').addEventListener('click', function () {
        done(false);
        dialog.close();
      });
      dialog.modal.querySelector('[data-role="ok"]').addEventListener('click', function () {
        done(true);
        dialog.close();
      });
    });
  }

  function openTokenModal(reason) {
    if (tokenModal) return tokenModal;
    var current = localStorage.getItem(TOKEN_KEY) || S.token || '';
    var reasonText = reason || '后端启用了 API 令牌校验（security.api_token），请输入访问令牌后继续。';
    var dialog = openModal(
      '<div class="modal-head"><span class="modal-ic">' + icon('i-key') + '</span><h2 class="modal-title">访问令牌</h2></div>' +
      '<p class="modal-text">' + esc(reasonText) + '</p>' +
      '<form id="token-form" novalidate>' +
      '<label class="field" for="token-input"><span>API 令牌</span>' +
      '<input id="token-input" type="password" autocomplete="current-password" placeholder="X-Api-Token 的值" value="' + escAttr(current) + '"></label>' +
      '<p class="modal-hint">令牌只保存在本机浏览器的 localStorage（键名 bts_token），之后所有 API 请求都会附带 X-Api-Token 请求头。</p>' +
      '<div class="modal-error" id="token-error" hidden></div>' +
      '<div class="modal-actions">' +
      '<button class="btn btn-ghost" type="button" data-role="clear">清除令牌</button>' +
      '<button class="btn" type="button" data-role="cancel">取消</button>' +
      '<button class="btn btn-primary" type="submit">保存并继续</button>' +
      '</div></form>',
      {
        label: '访问令牌',
        onClose: function () { tokenModal = null; }
      }
    );
    if (!dialog) return null;
    tokenModal = dialog;
    var errorEl = document.getElementById('token-error');
    var form = document.getElementById('token-form');

    function showError(message) {
      if (!errorEl) return;
      errorEl.hidden = false;
      errorEl.textContent = message;
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var input = document.getElementById('token-input');
      var value = input ? input.value.trim() : '';
      if (!value) {
        showError('请输入令牌；如果后端没有设置令牌，请联系管理员确认。');
        if (input) input.focus();
        return;
      }
      var submit = form.querySelector('[type="submit"]');
      busy(submit, function () {
        var previous = S.token;
        S.token = value;
        localStorage.setItem(TOKEN_KEY, value);
        return api('/api/status', { timeout: 8000, quiet: true }).then(function () {
          dialog.close();
          toast('访问令牌已保存');
          afterAuthRetry();
        }).catch(function (err) {
          if (err && err.code === 'unauthorized') {
            S.token = previous;
            if (previous) localStorage.setItem(TOKEN_KEY, previous);
            else localStorage.removeItem(TOKEN_KEY);
            showError('令牌无效，请检查后重试。');
          } else {
            showError(err && err.message ? err.message : '验证令牌失败');
          }
        });
      });
    });

    dialog.modal.querySelector('[data-role="cancel"]').addEventListener('click', function () { dialog.close(); });
    dialog.modal.querySelector('[data-role="clear"]').addEventListener('click', function () {
      localStorage.removeItem(TOKEN_KEY);
      S.token = '';
      dialog.close();
      toast('已清除本机保存的令牌', 'warn');
      afterAuthRetry();
    });
    return dialog;
  }

  function afterAuthRetry() {
    pollStatus();
    if (S.route) handleRoute();
  }

  /* ---------------------------------------------------------------- 通用渲染 / 加载态 */

  function busy(target, fn) {
    var btn = target;
    if (btn && btn.tagName === 'FORM') btn = btn.querySelector('[type="submit"]');
    if (!btn) return fn();
    if (btn.dataset && btn.dataset.busy === '1') return Promise.resolve();
    if (btn.dataset) btn.dataset.busy = '1';
    btn.classList.add('is-busy');
    btn.setAttribute('aria-busy', 'true');
    if ('disabled' in btn) btn.disabled = true;
    return Promise.resolve().then(fn).then(function (value) {
      return value;
    }, function (err) {
      console.error(err);
      toast('操作失败：' + (err && err.message ? err.message : '未知错误'), 'danger');
    }).then(function (value) {
      if (btn.dataset) btn.dataset.busy = '';
      btn.classList.remove('is-busy');
      btn.removeAttribute('aria-busy');
      if ('disabled' in btn) btn.disabled = false;
      return value;
    });
  }

  function pageHead(title, desc, actions) {
    var kickers = {
      '总览': 'SYSTEM / 01',
      '音箱': 'AUDIO / 02',
      '自动化': 'AUTOMATION / 03',
      '设置': 'CONFIG / 04',
      '新建规则': 'AUTOMATION / NEW',
      '编辑规则': 'AUTOMATION / EDIT'
    };
    return '<header class="chrome"><div class="chrome-main">' +
      '<p class="kicker">' + esc(kickers[title] || 'BTSpeaker') + '</p>' +
      '<h1 class="page-title">' + esc(title) + '</h1>' +
      (desc ? '<p class="page-desc">' + esc(desc) + '</p>' : '') +
      '</div>' + (actions ? '<div class="chrome-side">' + actions + '</div>' : '') + '</header>';
  }

  function cardHTML(opts) {
    var bodyClass = 'card-body' + (opts.flush ? ' card-body--flush' : '');
    return '<section class="card ' + (opts.cls || '') + '"' + (opts.id ? ' id="' + escAttr(opts.id) + '"' : '') + '>' +
      '<header class="card-head"><div class="card-title">' +
      (opts.iconName ? icon(opts.iconName) : '') +
      '<h2>' + esc(opts.title || '') + '</h2>' +
      (opts.subtitle ? '<span class="card-sub">' + esc(opts.subtitle) + '</span>' : '') +
      '</div>' + (opts.right ? '<div class="card-right">' + opts.right + '</div>' : '') + '</header>' +
      '<div class="' + bodyClass + '">' + (opts.body || '') + '</div></section>';
  }

  function pill(text, tone) {
    var tones = { ok: 'accent', accent: 'accent', warn: 'ghost', danger: 'ghost', muted: 'neutral' };
    return '<span class="tag tag--' + (tones[tone] || 'ghost') + '">' + esc(text) + '</span>';
  }

  function dot(tone, pulse) {
    return '<span class="dot ' + (tone || '') + (pulse ? ' pulse' : '') + '"></span>';
  }

  function emptyHTML(iconName, title, hint, extra) {
    return '<div class="empty">' +
      '<span class="empty-block" aria-hidden="true"></span>' +
      '<p class="empty-title">' + esc(title) + '</p>' +
      '<p class="empty-hint">' + esc(hint || '') + '</p>' +
      (extra ? '<div class="empty-extra">' + extra + '</div>' : '') +
      '</div>';
  }

  function kvRows(pairs) {
    return '<div class="kv">' + pairs.map(function (row) {
      return '<div class="kv-row"><span class="kv-k">' + esc(row[0]) + '</span>' +
        '<span class="kv-v ' + (row[2] ? 'mono' : '') + '">' + esc(row[1] == null || row[1] === '' ? '—' : row[1]) + '</span></div>';
    }).join('') + '</div>';
  }

  function skeletonGrid(count) {
    var total = count || 4;
    var html = '<div class="layout">';
    for (var i = 0; i < total; i += 1) {
      var span = i % 3 === 0 ? 'col-7' : i % 3 === 1 ? 'col-5' : 'col-12';
      html += '<div class="' + span + ' skeleton-card"></div>';
    }
    return html + '</div>';
  }

  function renderPageSkeleton(title, desc, actions) {
    render(pageHead(title, desc, actions) + skeletonGrid(6));
  }

  function renderOffline(title, desc) {
    render(pageHead(title, desc) +
      '<div class="layout"><div class="col-12"><div class="card">' +
      emptyHTML('i-alert', '暂时无法连接后端服务',
        '请确认 btspeaker 服务已经在路由器（或开发机）上启动，并且本机与它处于同一网络。服务恢复后点击重试。',
        '<button class="btn btn-primary" type="button" data-act="retry">' + icon('i-refresh') + '重试连接</button>') +
      '</div></div></div>');
  }

  function render(html) {
    var view = viewEl();
    if (view) view.innerHTML = html;
  }

  function renderRouteError(name, err) {
    render(pageHead(ROUTE_TITLES[name] || '页面', '页面加载失败') +
      '<div class="layout"><div class="col-12"><div class="card">' +
      emptyHTML('i-alert', '页面加载失败', (err && err.message) || '未知错误',
        '<button class="btn btn-primary" type="button" data-act="refresh">' + icon('i-refresh') + '重新加载</button>') +
      '</div></div></div>');
  }

  /* ---------------------------------------------------------------- 格式化：设备 / 规则 */

  function adapterPillHTML(st) {
    if (!st) return pill('未知', 'muted');
    var bt = st.bluetooth || {};
    if (st.sim) return pill('模拟适配器', 'accent');
    if (!bt.available) return pill('不可用', 'danger');
    if (!bt.powered) return pill('未开启', 'warn');
    return pill('已就绪', 'ok');
  }

  function connectedPillHTML(st) {
    var dev = st && st.bluetooth ? st.bluetooth.connected : null;
    return dev ? pill('已连接', 'ok') : pill('未连接', 'muted');
  }

  function adapterBody(st) {
    var bt = (st && st.bluetooth) || {};
    var adapter = bt.adapter || {};
    if (!bt.available) {
      return '<p class="kpi-value">不可用</p>' +
        '<p class="kpi-meta">请检查 bluez 与蓝牙适配器</p>';
    }
    var flags = [];
    if (st && st.sim) flags.push('SIM');
    if (bt.scanning) flags.push('SCANNING');
    if (!bt.powered) flags.push('POWER OFF');
    return '<p class="kpi-value">' + esc(adapter.name || adapter.hci || '蓝牙适配器') + '</p>' +
      '<p class="kpi-meta">' + esc(adapter.address || '—') +
      (flags.length ? ' · ' + esc(flags.join(' · ')) : '') + '</p>';
  }

  function connectedBody(st) {
    var dev = st && st.bluetooth ? st.bluetooth.connected : null;
    if (!dev) {
      return '<p class="kpi-value">未连接</p>' +
        '<p class="kpi-meta">等待音箱连接</p>';
    }
    return '<p class="kpi-value">' + esc(deviceLabel(dev)) + '</p>' +
      '<p class="kpi-meta">' + esc(dev.address || '') + '</p>';
  }

  function eqHTML(active) {
    var bars = '';
    for (var i = 0; i < 5; i += 1) bars += '<i></i>';
    return '<span class="eq' + (active ? ' is-on' : '') + '" aria-hidden="true">' + bars + '</span>';
  }

  function playerBody(st) {
    var pl = (st && st.player) || {};
    var current = pl.current || {};
    var playing = !!pl.playing;
    var volume = typeof pl.volume === 'number' ? pl.volume : 70;
    var title = current.label || current.source || '播放中';
    var probe = pl.probe || {};
    var body = '<div class="player">' +
      '<div class="player-top"><div>' +
      '<p class="player-title">' + esc(playing ? title : '空闲') + '</p>' +
      '<p class="player-meta">' + esc(playing ? (current.address || current.source || '') : '播放声音或语音播报后会显示在这里') + '</p>' +
      '</div>' + pill(playing ? '播放中' : '空闲', playing ? 'ok' : 'muted') + '</div>' +
      '<div class="volume">' +
      '<div class="volume-head"><label for="dash-volume">VOLUME / 音量</label>' +
      '<span class="volume-val" id="dash-volume-val">' + volume + '</span></div>' +
      '<input id="dash-volume" data-volume type="range" min="0" max="100" step="1" value="' + volume + '" aria-label="播放音量">' +
      '<div class="volume-scale"><span>0</span><span>50</span><span>100</span></div></div>' +
      '<div class="row row-end">' +
      '<button class="btn" type="button" data-act="player-stop"' + (playing ? '' : ' disabled') + '>' +
      icon('i-stop') + '停止播放</button>' +
      '<a class="btn btn-ghost" href="#/devices">播放控制</a></div>' +
      (probe && probe.ready === false
        ? '<p class="hint hint-warn">' + icon('i-alert') + esc(probe.hint || '播放后端未就绪') + '</p>'
        : '') +
      '</div>';
    return body;
  }

  function libraryOptions(selected) {
    if (!S.library.length) return '<option value="">（声音库为空）</option>';
    var seen = {};
    var out = '';
    S.library.forEach(function (item) {
      var value = item.path || item.name || '';
      if (!value || seen[value]) return;
      seen[value] = true;
      out += '<option value="' + escAttr(value) + '"' + (value === selected ? ' selected' : '') + '>' +
        esc(item.name || shortSource(value)) + (item.size_text ? ' · ' + esc(item.size_text) : '') + '</option>';
    });
    return out || '<option value="">（声音库为空）</option>';
  }

  function findLibraryItem(value) {
    var found = null;
    S.library.forEach(function (item) {
      if (item && (item.path || item.name) === value) found = item;
    });
    return found;
  }

  function selectedOptionLabel(select) {
    if (!select || !select.options || select.selectedIndex < 0) return '';
    var option = select.options[select.selectedIndex];
    return option ? String(option.textContent || '').replace(/\s+·\s+[^·]+$/, '').trim() : '';
  }

  function scanStatusText() {
    if (!S.scan) return '尚未扫描附近设备';
    if (S.scan.scanning) return '正在扫描附近设备…';
    if (S.scan.error) return '上次扫描失败：' + S.scan.error;
    var count = (S.scan.items || []).length;
    return '上次扫描发现 ' + count + ' 个设备';
  }

  function scanPillHTML() {
    if (!S.scan) return pill('未扫描', 'muted');
    if (S.scan.scanning) return pill('扫描中', 'accent');
    if (S.scan.error) return pill('扫描失败', 'danger');
    return pill('发现 ' + (S.scan.items || []).length + ' 个', 'ok');
  }

  function quickBody(st) {
    var tts = (st && st.tts) || {};
    return '<div class="quick-grid">' +
      '<form class="quick-tile" id="tts-quick-form" novalidate>' +
      '<p class="quick-k">01 / 语音播报</p>' +
      '<input id="tts-quick-text" type="text" maxlength="300" placeholder="输入要播报的文字…" value="' +
      escAttr(S.drafts.ttsText || '') + '" aria-label="要播报的文字">' +
      '<div class="row row-end"><button class="btn btn-primary" type="submit">' + icon('i-play') + '播报</button></div>' +
      '<p class="hint">TTS 后端：' + esc(tts.backend || '未配置') +
      (tts.ready === false ? '（未就绪）' : '') + '</p></form>' +

      '<div class="quick-tile">' +
      '<p class="quick-k">02 / 播放提示音</p>' +
      '<div class="quick-row"><select id="quick-sound" aria-label="选择声音库文件">' + libraryOptions(S.drafts.quickSound) + '</select>' +
      '<button class="btn" type="button" data-act="quick-play">' + icon('i-play') + '播放</button></div>' +
      '<p class="hint">声音库共 ' + S.library.length + ' 个文件</p></div>' +

      '<div class="quick-tile">' +
      '<p class="quick-k">03 / 扫描附近设备</p>' +
      '<div class="quick-row"><button class="btn" type="button" data-act="scan-start">' + icon('i-scan') + '开始扫描</button>' +
      '<a class="btn btn-ghost" href="#/devices">音箱页</a></div>' +
      '<p class="hint" id="quick-scan-status">' + esc(scanStatusText()) + '</p></div>' +

      '<div class="quick-tile">' +
      '<p class="quick-k">04 / 播放与自动化</p>' +
      '<div class="quick-row"><button class="btn btn-danger-ghost" type="button" data-act="player-stop">' +
      icon('i-stop') + '停止播放</button><a class="btn btn-ghost" href="#/rules">规则管理</a></div>' +
      '<p class="hint">规则 ' + S.ruleCounts.total + ' 条，' + S.ruleCounts.enabled + ' 条启用</p></div>' +
      '</div>';
  }

  function rulesStatsBody(st) {
    var counts = (st && st.rules) || {};
    var engine = (st && st.engine) || {};
    var total = Number(counts.total) || 0;
    var enabled = Number(counts.enabled) || 0;
    var percent = total ? Math.round(enabled / total * 100) : 0;
    return '<div class="stat-row">' +
      '<div class="stat"><span class="stat-num">' + total + '</span><span class="stat-label">TOTAL / 规则</span></div>' +
      '<div class="stat"><span class="stat-num accent">' + enabled + '</span><span class="stat-label">ENABLED / 启用</span></div>' +
      '<div class="stat"><span class="stat-num">' + (Number(engine.running) || 0) + '</span><span class="stat-label">RUNNING / 执行</span></div>' +
      '</div>' +
      '<div class="meter" role="img" aria-label="启用比例 ' + percent + '%"><span class="meter-fill" style="width:' + percent + '%"></span></div>' +
      '<p class="hint">调度器：' + (engine.scheduler_running === false ? '未运行' : '运行中') +
      (engine.queued ? ' · 队列 ' + engine.queued : '') + '</p>' +
      '<div><a class="link" href="#/rules">管理规则 →</a></div>';
  }

  function nextRunsBody(st) {
    var runs = (st && st.engine && st.engine.next_runs) || [];
    if (!runs.length) {
      return emptyHTML('i-clock', '暂无待触发的定时任务',
        '创建「定时」触发的规则后，这里会列出下一次执行时间。',
        '<a class="btn btn-sm" href="#/rules/new">' + icon('i-plus') + '新建定时规则</a>');
    }
    return '<div class="table-wrap"><table class="table table--compact next-table">' +
      '<thead><tr><th>时间</th><th>规则</th><th class="num">剩余</th></tr></thead><tbody>' +
      runs.slice(0, 5).map(function (run) {
        var inText = '';
        if (typeof run.in === 'number') inText = fmtIn(run.in);
        else if (run.at) inText = fmtIn((toTime(run.at) - Date.now()) / 1000);
        return '<tr>' +
          '<td class="num-left">' + esc(fmtClock(run.at).slice(0, 5)) + '</td>' +
          '<td class="name">' + esc(run.name || run.rule_id || '规则') + '</td>' +
          '<td class="num next-in">' + esc(inText) + '</td></tr>';
      }).join('') +
      '</tbody></table></div>';
  }

  function logsBody() {
    var items = S.logs.slice(0, 8);
    if (!items.length) {
      return emptyHTML('i-list', '暂无日志', '服务运行后，日志会显示在这里。');
    }
    return '<div class="table-wrap"><table class="table table--compact log-table">' +
      '<colgroup><col class="lt-time"><col class="lt-level"><col class="lt-msg"></colgroup>' +
      '<thead><tr><th>时间</th><th>级别</th><th>信息</th></tr></thead><tbody>' +
      items.map(function (log) {
        var tone = levelTone(log.level);
        return '<tr>' +
          '<td class="time">' + esc(fmtClock(log.time)) + '</td>' +
          '<td><span class="log-level ' + tone + '">' + esc(String(log.level || 'INFO').toUpperCase()) + '</span></td>' +
          '<td class="log-msg" title="' + escAttr(log.message || '') + '">' + esc(log.message || '') + '</td></tr>';
      }).join('') +
      '</tbody></table></div>';
  }

  /* ---------------------------------------------------------------- 总览页 */

  function playingKpiHTML(st) {
    var pl = (st && st.player) || {};
    var current = pl.current || {};
    var volume = typeof pl.volume === 'number' ? pl.volume : 70;
    if (!pl.playing) {
      return '<p class="kpi-value">空闲</p>' +
        '<p class="kpi-meta">' + esc(pl.backend || '未配置') + ' · 音量 ' + volume + '</p>';
    }
    return '<p class="kpi-value">播放中</p>' +
      '<p class="kpi-meta">' + esc(current.label || current.source || '播放任务') + ' · 音量 ' + volume + '</p>';
  }

  function rulesKpiHTML(st) {
    var counts = (st && st.rules) || {};
    var total = Number(counts.total) || 0;
    var enabled = Number(counts.enabled) || 0;
    return '<p class="kpi-value kpi-value--lg">' + enabled + '</p>' +
      '<p class="kpi-meta">已启用 / 共 ' + total + ' 条</p>';
  }

  function renderDashboard() {
    var st = S.status;
    var playing = !!(st && st.player && st.player.playing);
    render(
      pageHead('总览', '适配器、播放与自动化任务的实时概览',
        '<button class="btn" type="button" data-act="refresh">' + icon('i-refresh') + '刷新</button>') +
      '<div class="kpi-strip">' +
        '<div class="kpi"><span class="kpi-label">ADAPTER / 适配器</span>' +
        '<div id="dash-adapter-body">' + adapterBody(st) + '</div>' +
        '<div class="tag-row" id="dash-adapter-pill-wrap">' + adapterPillHTML(st) + '</div></div>' +
        '<div class="kpi"><span class="kpi-label">SPEAKER / 当前音箱</span>' +
        '<div id="dash-connected-body">' + connectedBody(st) + '</div>' +
        '<div class="tag-row" id="dash-connected-pill-wrap">' + connectedPillHTML(st) + '</div></div>' +
        '<div class="kpi"><span class="kpi-label">PLAYBACK / 播放</span>' +
        '<div id="dash-playing-kpi">' + playingKpiHTML(st) + '</div></div>' +
        '<div class="kpi"><span class="kpi-label">AUTOMATION / 规则</span>' +
        '<div id="dash-rules-kpi">' + rulesKpiHTML(st) + '</div></div>' +
      '</div>' +
      '<div class="layout">' +
        '<div class="col-7 stack">' +
        cardHTML({
          iconName: 'i-music', title: '当前播放',
          right: '<span id="dash-eq-wrap">' + eqHTML(playing) + '</span>',
          body: '<div id="dash-player-body">' + playerBody(st) + '</div>'
        }) +
        cardHTML({
          iconName: 'i-zap', title: '快捷动作',
          body: '<div id="dash-quick-body">' + quickBody(st) + '</div>'
        }) +
        '</div>' +
        '<div class="col-5 stack">' +
        cardHTML({
          iconName: 'i-clock', title: '下一批定时任务', flush: true,
          right: '<a class="link" href="#/rules">全部规则</a>',
          body: '<div id="dash-next-runs">' + nextRunsBody(st) + '</div>'
        }) +
        cardHTML({
          iconName: 'i-list', title: '最近日志', flush: true,
          right: '<button class="btn btn-sm btn-ghost" type="button" data-act="refresh-logs">' + icon('i-refresh') + '刷新</button>',
          body: '<div id="dash-logs-body">' + logsBody() + '</div>'
        }) +
        cardHTML({
          iconName: 'i-rules', title: '规则统计',
          body: '<div id="dash-rules-stats">' + rulesStatsBody(st) + '</div>'
        }) +
        '</div>' +
      '</div>' +
      (st && st.sim
        ? '<p class="hint">' + icon('i-info') + '模拟模式：蓝牙、播放器与 TTS 使用模拟后端，不会操作真实硬件。</p>'
        : '')
    );
    syncVolumeSliders();
  }

  function syncDashboard(st) {
    if (!st) return;
    patchById('dash-adapter-body', adapterBody(st));
    patchById('dash-connected-body', connectedBody(st));
    patchById('dash-playing-kpi', playingKpiHTML(st));
    patchById('dash-rules-kpi', rulesKpiHTML(st));
    patchIfIdle('dash-player-body', playerBody(st));
    patchById('dash-rules-stats', rulesStatsBody(st));
    patchById('dash-next-runs', nextRunsBody(st));
    var wrap = document.getElementById('dash-adapter-pill-wrap');
    if (wrap) wrap.innerHTML = adapterPillHTML(st);
    var wrap2 = document.getElementById('dash-connected-pill-wrap');
    if (wrap2) wrap2.innerHTML = connectedPillHTML(st);
    var eqWrap = document.getElementById('dash-eq-wrap');
    if (eqWrap) eqWrap.innerHTML = eqHTML(!!(st.player && st.player.playing));
    setText('quick-scan-status', scanStatusText());
    syncVolumeSliders();
  }

  var dashboardRoute = {
    mount: function (parsed, seq) {
      renderPageSkeleton('总览', '适配器、播放与自动化任务的实时概览',
        '<button class="btn" type="button" data-act="refresh">' + icon('i-refresh') + '刷新</button>');
      return ensureStatus().then(function (st) {
        if (seq !== S.navSeq) return;
        if (!st) {
          S.routeOffline = true;
          renderOffline('总览', '无法读取运行状态');
          return;
        }
        S.routeOffline = false;
        S.ruleCounts = {
          total: Number((st.rules || {}).total) || 0,
          enabled: Number((st.rules || {}).enabled) || 0
        };
        return Promise.all([
          loadLogs(10).catch(function () { return null; }),
          loadLibrary().catch(function () { return null; })
        ]).then(function () {
          if (seq !== S.navSeq) return;
          renderDashboard();
          var timer = setInterval(function () {
            loadLogs(10).then(function () {
              var node = document.getElementById('dash-logs-body');
              if (node) patchHTML(node, logsBody());
            }).catch(function () { /* 轮询失败静默 */ });
          }, 5000);
          routeTimers.push(timer);
        });
      });
    },
    sync: syncDashboard
  };

  /* ---------------------------------------------------------------- 音箱页 */

  function deviceItems() {
    if (S.deviceData && Array.isArray(S.deviceData.items)) return S.deviceData.items;
    var bt = (S.status && S.status.bluetooth) || {};
    return Array.isArray(bt.devices) ? bt.devices : [];
  }

  function connectedDevice() {
    if (S.deviceData && S.deviceData.connected) return S.deviceData.connected;
    var bt = (S.status && S.status.bluetooth) || {};
    return bt.connected || null;
  }

  function deviceListHTML() {
    var items = deviceItems().slice();
    if (!items.length) {
      return emptyHTML('i-speaker', '还没有配对音箱',
        '点右上角「扫描附近设备」，发现周围的蓝牙音箱后点击「配对」。配对完成后即可连接与播放。',
        '<button class="btn" type="button" data-act="scan-start">' + icon('i-scan') + '扫描附近设备</button>');
    }
    items.sort(function (a, b) {
      if (!!b.connected !== !!a.connected) return b.connected ? 1 : -1;
      if (!!b.paired !== !!a.paired) return b.paired ? 1 : -1;
      return String(a.name || a.address).localeCompare(String(b.name || b.address), 'zh-Hans-CN');
    });
    return '<div class="table-wrap"><table class="table device-table">' +
      '<colgroup><col class="dt-name"><col class="dt-addr"><col class="dt-state"><col class="dt-ops"></colgroup>' +
      '<thead><tr><th>音箱</th><th>地址</th><th>状态</th><th class="col-actions">操作</th></tr></thead><tbody>' +
      items.map(function (dev) {
        var isDefault = S.defaultDevice && dev.address === S.defaultDevice;
        var state = dev.connected ? pill('已连接', 'ok') : pill(dev.paired ? '已配对' : '未配对', 'muted');
        var label = deviceLabel(dev);
        function iconButton(act, iconName, title, extraClass) {
          return '<button class="btn btn-sm btn-icon' + (extraClass || '') + '" type="button" data-act="' + act + '"' +
            ' data-addr="' + escAttr(dev.address) + '" data-name="' + escAttr(label) + '"' +
            ' title="' + escAttr(title) + '" aria-label="' + escAttr(title + ' ' + label) + '">' +
            icon(iconName) + '</button>';
        }
        var actions = dev.connected
          ? iconButton('device-disconnect', 'i-link-off', '断开连接')
          : iconButton('device-connect', 'i-link', '连接');
        if (dev.paired && !isDefault) actions += iconButton('device-default', 'i-star', '设为默认');
        actions += iconButton('device-remove', 'i-trash', '删除设备', ' btn-danger-ghost');
        return '<tr>' +
          '<td><div class="device-mini"><span class="device-ic">' + icon('i-speaker') + '</span>' +
          '<div class="device-main"><div class="device-name">' +
          '<span class="device-label">' + esc(label) + '</span>' +
          (isDefault ? '<span class="tag tag--ghost">默认</span>' : '') + '</div>' +
          '<div class="device-meta">' + (dev.trusted ? '已信任' : '未信任') + '</div></div></div></td>' +
          '<td class="num-left">' + esc(dev.address || '') + '</td>' +
          '<td>' + state + '</td>' +
          '<td class="col-actions"><div class="device-actions">' + actions + '</div></td></tr>';
      }).join('') +
      '</tbody></table></div>';
  }

  function devPlayerBody(st) {
    var pl = (st && st.player) || {};
    var volume = typeof pl.volume === 'number' ? pl.volume : 70;
    var probe = pl.probe || {};
    return '<div class="volume">' +
      '<div class="volume-head"><label for="dev-volume">VOLUME / 音量</label>' +
      '<span class="volume-val" id="dev-volume-val">' + volume + '</span></div>' +
      '<input id="dev-volume" data-volume type="range" min="0" max="100" step="1" value="' + volume + '" aria-label="播放音量">' +
      '<div class="volume-scale"><span>0</span><span>50</span><span>100</span></div></div>' +
      '<label class="field"><span>SOUND LIBRARY / 声音库</span>' +
      '<select id="dev-sound" aria-label="选择要播放的声音">' + libraryOptions(S.drafts.devSound) + '</select></label>' +
      '<div class="row"><button class="btn" type="button" data-act="library-upload">' + icon('i-upload') + '上传声音</button>' +
      '<button class="btn" type="button" data-act="dev-play">' + icon('i-play') + '播放</button>' +
      '<button class="btn btn-ghost" type="button" data-act="player-stop">' + icon('i-stop') + '停止</button></div>' +
      '<input type="file" id="library-upload-input" accept="audio/*" hidden tabindex="-1" aria-hidden="true">' +
      '<p class="hint">播放后端：' + esc(pl.backend || '未配置') +
      (probe && probe.ready === false ? ' · ' + esc(probe.hint || '未就绪') : '') + '</p>';
  }

  function scanBlockHTML() {
    var scan = S.scan;
    if (!scan) {
      return emptyHTML('i-scan', '尚未扫描附近设备',
        '点右上角「扫描附近设备」开始发现周围可配对的蓝牙音箱，扫描大约需要 12 秒。');
    }
    var items = scan.items || [];
    var head = '';
    if (scan.scanning) {
      head = '<div class="scan-progress" aria-hidden="true"></div>' +
        '<div class="scan-status"><span class="spinner" aria-hidden="true"></span>' +
        '<span>正在扫描附近蓝牙设备…已发现 ' + items.length + ' 个，请稍候</span></div>';
    } else if (scan.error) {
      head = '<div class="scan-status">' + icon('i-alert') +
        '<span>扫描失败：' + esc(scan.error) + '，请重试或检查蓝牙适配器。</span></div>';
    } else {
      head = '<div class="scan-head"><span>扫描完成，共发现 ' + items.length + ' 个设备' +
        (scan.started_at ? '（' + esc(fmtAgo(scan.started_at)) + '）' : '') + '</span>' +
        '<button class="btn btn-sm btn-ghost" type="button" data-act="scan-start">' + icon('i-refresh') + '重新扫描</button></div>';
    }
    if (items.length) {
      head += '<div class="table-wrap"><table class="table">' +
        '<thead><tr><th>设备</th><th>地址</th><th>状态</th><th class="col-actions">操作</th></tr></thead><tbody>' +
        items.map(function (dev) {
          var isKnown = dev.paired;
          var state = dev.connected ? pill('已连接', 'ok') : (dev.paired ? pill('已配对', 'muted') : pill('可配对', 'muted'));
          return '<tr>' +
            '<td class="name">' + esc(deviceLabel(dev)) + '</td>' +
            '<td class="num-left">' + esc(dev.address || '') + '</td>' +
            '<td>' + state + '</td>' +
            '<td class="col-actions">' +
            (!isKnown
              ? '<button class="btn btn-sm" type="button" data-act="device-pair" data-addr="' + escAttr(dev.address) + '" data-name="' + escAttr(deviceLabel(dev)) + '">' + icon('i-link') + '配对</button>'
              : (dev.connected
                ? '<button class="btn btn-sm btn-ghost" type="button" data-act="device-disconnect" data-addr="' + escAttr(dev.address) + '" data-name="' + escAttr(deviceLabel(dev)) + '">断开</button>'
                : '<button class="btn btn-sm" type="button" data-act="device-connect" data-addr="' + escAttr(dev.address) + '" data-name="' + escAttr(deviceLabel(dev)) + '">' + icon('i-link') + '连接</button>')) +
            '</td></tr>';
        }).join('') +
        '</tbody></table></div>';
    } else if (!scan.scanning && !scan.error) {
      head += emptyHTML('i-scan', '没有发现新设备',
        '确认音箱已经进入配对模式并靠近路由器，然后重新扫描。');
    }
    return head;
  }

  function renderDevicesPage() {
    var st = S.status;
    var bt = (st && st.bluetooth) || {};
    render(
      pageHead('音箱', '配对、连接、音量与播放控制',
        '<button class="btn" type="button" data-act="refresh">' + icon('i-refresh') + '刷新</button>' +
        '<button class="btn btn-primary" type="button" data-act="scan-start">' + icon('i-scan') + '扫描附近设备</button>') +
      '<div class="info-bar">' +
      '<div class="info-item"><span class="info-k">ADAPTER / 适配器</span>' +
      '<div id="dev-adapter-body">' + adapterBody(st) + '</div></div>' +
      '<div class="info-item"><span class="info-k">STATE / 状态</span>' +
      '<span class="info-v">' + (bt.powered ? '已上电' : '未上电') + '</span></div>' +
      '<div class="info-item"><span class="info-k">SCAN / 扫描</span>' +
      '<span class="info-v">' + (bt.scanning ? '扫描中' : '空闲') + '</span></div>' +
      '<div class="info-actions"><span id="dev-adapter-pill-wrap">' + adapterPillHTML(st) + '</span></div>' +
      '</div>' +
      '<div class="layout">' +
      cardHTML({
        cls: 'col-7', iconName: 'i-speaker', title: '已配对音箱', flush: true,
        right: '<span class="card-sub" id="dev-count">' + deviceItems().length + ' 台</span>',
        body: '<div id="dev-list">' + deviceListHTML() + '</div>'
      }) +
      cardHTML({
        cls: 'col-5', iconName: 'i-volume', title: '音量与播放',
        body: '<div id="dev-player-body">' + devPlayerBody(st) + '</div>'
      }) +
      cardHTML({
        cls: 'col-12', iconName: 'i-scan', title: '附近设备', flush: true,
        right: '<span id="dev-scan-pill-wrap">' + scanPillHTML() + '</span>',
        body: '<div id="dev-scan-body">' + scanBlockHTML() + '</div>'
      }) +
      '</div>'
    );
    syncVolumeSliders();
  }

  function syncDevices(st) {
    if (!st) return;
    patchById('dev-adapter-body', adapterBody(st));
    patchIfIdle('dev-player-body', devPlayerBody(st));
    patchById('dev-list', deviceListHTML());
    patchById('dev-scan-body', scanBlockHTML());
    setText('dev-count', deviceItems().length + ' 台');
    var wrap = document.getElementById('dev-adapter-pill-wrap');
    if (wrap) wrap.innerHTML = adapterPillHTML(st);
    var wrap2 = document.getElementById('dev-scan-pill-wrap');
    if (wrap2) wrap2.innerHTML = scanPillHTML();
    syncVolumeSliders();
  }

  function updateScanViews() {
    patchById('dev-scan-body', scanBlockHTML());
    var wrap = document.getElementById('dev-scan-pill-wrap');
    if (wrap) wrap.innerHTML = scanPillHTML();
    setText('quick-scan-status', scanStatusText());
  }

  var devicesRoute = {
    mount: function (parsed, seq) {
      renderPageSkeleton('音箱', '配对、连接、音量与播放控制',
        '<button class="btn" type="button" data-act="refresh">' + icon('i-refresh') + '刷新</button>' +
        '<button class="btn btn-primary" type="button" data-act="scan-start">' + icon('i-scan') + '扫描附近设备</button>');
      return ensureStatus().then(function (st) {
        if (seq !== S.navSeq) return;
        if (!st) {
          S.routeOffline = true;
          renderOffline('音箱', '无法读取设备状态');
          return;
        }
        S.routeOffline = false;
        return Promise.all([
          refreshDeviceData().catch(function () { return null; }),
          loadLibrary().catch(function () { return null; }),
          loadDefaultDevice()
        ]).then(function () {
          if (seq !== S.navSeq) return;
          renderDevicesPage();
          var timer = setInterval(function () {
            refreshDeviceData().then(function () {
              if (S.route === 'devices') syncDevices(S.status);
            }).catch(function () { /* 静默 */ });
          }, 5000);
          routeTimers.push(timer);
          if (S.scan && S.scan.scanning) startScanPolling();
        });
      });
    },
    sync: syncDevices
  };

  /* ---------------------------------------------------------------- 数据加载 */

  function loadLogs(limit) {
    return api('/api/logs?limit=' + encodeURIComponent(limit || 20), { timeout: 10000 }).then(function (data) {
      S.logs = (data && Array.isArray(data.items)) ? data.items : [];
      return S.logs;
    });
  }

  function loadLibrary(force) {
    if (!force && S.libraryLoaded) return Promise.resolve(S.library);
    return api('/api/library', { timeout: 12000 }).then(function (data) {
      S.library = (data && Array.isArray(data.items)) ? data.items : [];
      S.libraryLoaded = true;
      return S.library;
    });
  }

  // 上传成功后刷新所有引用声音库的界面（当前页下拉、快捷动作、编辑页 datalist）。
  function refreshLibraryViews() {
    if (S.route === 'devices') {
      syncDevices(S.status);
    } else if (S.route === 'dashboard') {
      var quick = document.getElementById('dash-quick-body');
      if (quick) patchHTML(quick, quickBody(S.status));
      setText('quick-scan-status', scanStatusText());
      syncVolumeSliders();
    }
    var datalist = document.getElementById('sound-sources');
    if (datalist) datalist.innerHTML = soundDatalistOptions();
  }

  function uploadLibraryFile(file, btn) {
    if (!file) return undefined;
    return busy(btn, function () {
      return apiUpload('/api/library/upload?name=' + encodeURIComponent(file.name), file, { timeout: 120000 })
        .then(function (data) {
          var savedName = data && data.name ? data.name : file.name;
          if (data && data.path) S.drafts.devSound = data.path;
          toast('已保存到声音库：' + savedName);
          return loadLibrary(true).then(function () {
            refreshLibraryViews();
          }).catch(function () {
            refreshLibraryViews();
          });
        })
        .catch(function (err) {
          if (err && err.code === 'payload_too_large') toast('文件超过 20MB 上限', 'danger');
          else toast(err && err.message ? err.message : '上传失败', 'danger');
        });
    });
  }

  function refreshDeviceData() {
    return api('/api/bluetooth/devices', { timeout: 12000 }).then(function (data) {
      if (data && Array.isArray(data.items)) S.deviceData = data;
      return S.deviceData;
    });
  }

  function loadDefaultDevice() {
    if (!S.settingsConfig) {
      return loadSettingsConfig(false).then(function (cfg) {
        S.defaultDevice = (cfg && cfg.bluetooth && cfg.bluetooth.default_device) || '';
      }).catch(function () { /* 忽略 */ });
    }
    S.defaultDevice = (S.settingsConfig.bluetooth && S.settingsConfig.bluetooth.default_device) || '';
    return Promise.resolve();
  }

  function loadRules() {
    return api('/api/rules', { timeout: 12000 }).then(function (data) {
      S.rules = (data && Array.isArray(data.items)) ? data.items : [];
      var counts = (data && data.counts) || {};
      S.ruleCounts = {
        total: Number(counts.total) >= 0 && counts.total != null ? Number(counts.total) : S.rules.length,
        enabled: Number(counts.enabled) >= 0 && counts.enabled != null ? Number(counts.enabled) : S.rules.filter(function (r) { return r.enabled; }).length
      };
      S.rulesLoaded = true;
      return S.rules;
    });
  }

  function loadEvents() {
    return api('/api/events?limit=50', { timeout: 10000 }).then(function (data) {
      S.events = (data && Array.isArray(data.items)) ? data.items : [];
      return S.events;
    });
  }

  function loadSettingsConfig(force) {
    if (!force && S.settingsConfig) return Promise.resolve(S.settingsConfig);
    return api('/api/settings', { timeout: 15000 }).then(function (data) {
      S.settingsConfig = (data && data.config) ? data.config : {};
      return S.settingsConfig;
    });
  }

  function loadSystem(force) {
    if (!force && S.system) return Promise.resolve(S.system);
    return api('/api/system/info', { timeout: 10000 }).then(function (data) {
      S.system = data || {};
      return S.system;
    });
  }

  function loadDiag(force) {
    if (!force && S.diag) return Promise.resolve(S.diag);
    return api('/api/diag', { timeout: 20000 }).then(function (data) {
      S.diag = data || {};
      return S.diag;
    });
  }

  /* ---------------------------------------------------------------- 音量 */

  function syncVolumeSliders() {
    $$('[data-volume]').forEach(function (input) {
      var value = clampInt(input.value, 0, 100);
      input.style.setProperty('--fill', value + '%');
    });
  }

  function updateVolumeLabels(value) {
    var v = clampInt(value, 0, 100);
    setText('dash-volume-val', String(v));
    setText('dev-volume-val', String(v));
    $$('[data-volume]').forEach(function (input) {
      if (document.activeElement !== input) input.value = String(v);
      input.style.setProperty('--fill', v + '%');
    });
  }

  function setVolumeFromInput(input) {
    var level = clampInt(input.value, 0, 100);
    updateVolumeLabels(level);
    return api('/api/player/volume', { method: 'POST', body: { level: level }, timeout: 12000 }).then(function (data) {
      var applied = data && typeof data.volume === 'number' ? data.volume : level;
      updateVolumeLabels(applied);
      if (S.status && S.status.player) S.status.player.volume = applied;
      toast('音量已设为 ' + applied);
    }).catch(function (err) {
      toast(err.message, 'danger');
      pollStatus();
    });
  }

  /* ---------------------------------------------------------------- 播放 / 扫描 / 设备操作 */

  function playSource(btn, source, label) {
    if (!source) {
      toast('先选择一个声音库文件', 'warn');
      return undefined;
    }
    var name = label || shortSource(source);
    return busy(btn, function () {
      return api('/api/player/play', {
        method: 'POST',
        body: { source: source, label: name },
        timeout: 15000
      }).then(function (data) {
        if (data && data.accepted === false) toast('播放被拒绝：' + (data.reason || '未知原因'), 'warn');
        else toast('已提交播放任务：' + name);
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function stopPlayer(btn) {
    return busy(btn, function () {
      return api('/api/player/stop', { method: 'POST', body: {}, timeout: 12000 }).then(function () {
        toast('已停止播放');
        pollStatus();
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function submitQuickTts(form) {
    var input = document.getElementById('tts-quick-text');
    var text = input ? input.value.trim() : '';
    if (!text) {
      if (input) { input.classList.add('is-invalid'); input.focus(); }
      toast('请输入要播报的文字', 'warn');
      return;
    }
    var btn = form.querySelector('[type="submit"]');
    busy(btn, function () {
      return api('/api/tts/speak', { method: 'POST', body: { text: text }, timeout: 20000 }).then(function () {
        toast('已提交语音播报任务');
        S.drafts.ttsText = '';
        if (input) {
          input.value = '';
          input.classList.remove('is-invalid');
        }
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function startScan(btn) {
    return busy(btn, function () {
      return api('/api/bluetooth/scan', { method: 'POST', body: { timeout: 12 }, timeout: 15000 }).then(function () {
        S.scan = { scanning: true, items: [], started_at: new Date().toISOString(), error: '' };
        toast('正在扫描附近蓝牙设备，约 12 秒…', 'info');
        updateScanViews();
        startScanPolling();
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function startScanPolling() {
    stopScanPolling();
    var ticks = 0;
    scanTimer = setInterval(function () {
      ticks += 1;
      api('/api/bluetooth/scan', { timeout: 8000, quiet: true }).then(function (data) {
        S.scan = data || { scanning: false, items: [], error: '' };
        updateScanViews();
        if (!S.scan.scanning || ticks > 40) {
          stopScanPolling();
          var items = S.scan.items || [];
          if (S.scan.error) toast('扫描失败：' + S.scan.error, 'danger');
          else toast('扫描完成，发现 ' + items.length + ' 个蓝牙设备');
          refreshDeviceData().then(function () {
            if (S.route === 'devices') syncDevices(S.status);
            updateScanViews();
          }).catch(function () { /* 忽略 */ });
          pollStatus();
        }
      }).catch(function (err) {
        if (err && err.code === 'unauthorized') { stopScanPolling(); return; }
        stopScanPolling();
        toast('读取扫描状态失败：' + (err && err.message ? err.message : '未知错误'), 'danger');
        updateScanViews();
      });
    }, 1500);
  }

  function afterDeviceChange() {
    return pollStatus().then(function () {
      return refreshDeviceData().catch(function () { return null; });
    }).then(function () {
      if (S.route === 'devices') syncDevices(S.status);
      updateScanViews();
    });
  }

  function deviceAction(btn, kind, address, name) {
    if (!address) { toast('缺少设备地址', 'danger'); return undefined; }
    var path = kind === 'connect' ? '/api/bluetooth/connect' : '/api/bluetooth/disconnect';
    var verb = kind === 'connect' ? '已连接' : '已断开';
    return busy(btn, function () {
      return api(path, { method: 'POST', body: { address: address }, timeout: 90000 }).then(function () {
        return afterDeviceChange();
      }).then(function () {
        toast(verb + (name ? '「' + name + '」' : ''));
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function pairDevice(btn, address, name) {
    if (!address) { toast('缺少设备地址', 'danger'); return undefined; }
    return busy(btn, function () {
      return api('/api/bluetooth/pair', { method: 'POST', body: { address: address }, timeout: 45000 }).then(function () {
        return afterDeviceChange();
      }).then(function () {
        toast('配对成功' + (name ? '：' + name : '') + '，可以点击「连接」');
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function setDefaultDevice(btn, address, name) {
    if (!address) { toast('缺少设备地址', 'danger'); return undefined; }
    return busy(btn, function () {
      return api('/api/bluetooth/default', { method: 'POST', body: { address: address }, timeout: 15000 }).then(function () {
        S.defaultDevice = address;
        if (S.settingsConfig && S.settingsConfig.bluetooth) S.settingsConfig.bluetooth.default_device = address;
        toast('已设为默认音箱' + (name ? '：' + name : ''));
        if (S.route === 'devices') syncDevices(S.status);
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function removeDevice(btn, address, name) {
    if (!address) { toast('缺少设备地址', 'danger'); return undefined; }
    return confirmDialog({
      title: '删除已配对音箱？',
      message: '将从系统中移除「' + (name || address) + '」的配对记录，之后需要重新扫描并配对。',
      confirmText: '删除设备',
      danger: true
    }).then(function (ok) {
      if (!ok) return undefined;
      return busy(btn, function () {
        return api('/api/bluetooth/devices/' + encodeURIComponent(address), { method: 'DELETE', timeout: 20000 }).then(function () {
          return afterDeviceChange();
        }).then(function () {
          toast('设备已删除');
        }).catch(function (err) {
          toast(err.message, 'danger');
        });
      });
    });
  }

  /* ---------------------------------------------------------------- 自动化：规则列表 */

  function triggerSummary(trigger) {
    var t = trigger || {};
    if (t.type === 'schedule') {
      if (t.every_minutes != null) return '每 ' + t.every_minutes + ' 分钟';
      var days = Array.isArray(t.days) ? t.days : [];
      var when = days.length
        ? days.slice().sort(function (a, b) { return a - b; }).map(function (d) { return DAY_NAMES[d] || ''; }).join('、')
        : '每天';
      return when + ' ' + (t.at || '--:--');
    }
    if (t.type === 'bluetooth') {
      var ev = BT_EVENT_LABELS[t.event] || ('蓝牙事件 ' + (t.event || ''));
      return ev + '时（' + (t.device ? t.device : '任意设备') + '）';
    }
    if (t.type === 'boot') return '开机 ' + (t.delay != null ? t.delay : 0) + ' 秒后';
    if (t.type === 'manual') return '仅手动 / HTTP 钩子触发';
    return '未设置触发器';
  }

  function actionSummary(action) {
    var a = action || {};
    switch (a.type) {
      case 'play': return '播放：' + shortSource(a.source || '') + (a.volume != null ? ' @' + a.volume : '');
      case 'tts': return '语音播报';
      case 'volume': return '音量 → ' + (a.level != null ? a.level : '—');
      case 'connect': return a.address ? '连接 ' + a.address : '连接默认音箱';
      case 'disconnect': return '断开音箱';
      case 'http': return 'HTTP ' + (a.method || 'GET');
      case 'wait': return '等待 ' + (a.seconds != null ? a.seconds : 0) + ' 秒';
      case 'log': return '写日志';
      case 'command': return '执行命令';
      default: return String(a.type || '未知动作');
    }
  }

  function conditionsSummary(conditions) {
    if (!Array.isArray(conditions) || !conditions.length) return '';
    return conditions.map(function (c) {
      if (c.type === 'connected') return c.value === false ? '需未连接' : '需已连接';
      if (c.type === 'time_between') return '时间 ' + (c.start || '') + '–' + (c.end || '');
      if (c.type === 'weekday_in') {
        var days = (c.days || []).slice().sort(function (a, b) { return a - b; })
          .map(function (d) { return DAY_NAMES[d] || ''; }).join('、');
        return '星期：' + (days || '—');
      }
      return String(c.type || '');
    }).join(' · ');
  }

  function lastRunsMap() {
    var map = {};
    S.events.forEach(function (item) {
      if (!item) return;
      var id = item.rule_id;
      if (!id) return;
      var at = item.started_at || item.at || item.time || '';
      if (!map[id] || toTime(at) > toTime(map[id])) map[id] = at;
    });
    return map;
  }

  function ruleRowHTML(rule, lastRun, index) {
    var actions = Array.isArray(rule.actions) ? rule.actions : [];
    var conditions = conditionsSummary(rule.conditions);
    var chips = actions.slice(0, 2).map(function (action) {
      return '<span class="chip">' + esc(actionSummary(action)) + '</span>';
    }).join('');
    if (actions.length > 2) chips += '<span class="chip">+' + (actions.length - 2) + '</span>';
    return '<tr' + (rule.enabled ? '' : ' class="is-off"') + '>' +
      '<td class="num-left">R-' + pad2((index || 0) + 1) + '</td>' +
      '<td><span class="name">' + esc(rule.name || '未命名规则') + '</span>' +
      (rule.cooldown ? '<span class="sub">冷却 ' + rule.cooldown + ' 秒</span>' : '') +
      (conditions ? '<span class="sub">条件：' + esc(conditions) + '</span>' : '') + '</td>' +
      '<td class="rule-trigger">' + esc(triggerSummary(rule.trigger)) + '</td>' +
      '<td><div class="rule-chips">' + chips + '</div></td>' +
      '<td class="time">' + (lastRun ? esc(fmtAgo(lastRun)) + '<span class="sub">' + esc(fmtDateTime(lastRun)) + '</span>' : '—') + '</td>' +
      '<td><button class="switch" type="button" role="switch" aria-checked="' + (rule.enabled ? 'true' : 'false') + '"' +
      ' aria-label="' + escAttr((rule.name || '规则') + (rule.enabled ? '，已启用，点击停用' : '，已停用，点击启用')) + '"' +
      ' data-act="rule-toggle" data-id="' + escAttr(rule.id) + '"><span class="switch-thumb"></span></button></td>' +
      '<td class="col-actions">' +
      '<button class="btn btn-sm" type="button" data-act="rule-run" data-id="' + escAttr(rule.id) + '">' + icon('i-play') + '立即执行</button>' +
      '<a class="btn btn-sm btn-ghost" href="#/rules/edit/' + encodeURIComponent(rule.id) + '">' + icon('i-edit') + '编辑</a>' +
      '<button class="btn btn-sm btn-danger-ghost" type="button" data-act="rule-delete" data-id="' + escAttr(rule.id) + '" aria-label="删除规则 ' + escAttr(rule.name || '') + '">' + icon('i-trash') + '</button>' +
      '</td></tr>';
  }

  function rulesListHTML() {
    if (!S.rules.length) {
      return emptyHTML('i-rules', '还没有自动化规则',
        '创建规则后，可以按时间、蓝牙事件或开机自动播放声音、语音播报、调整音量，也可以从其他设备导入规则 JSON。',
        '<a class="link" href="#/rules/new">创建第一条规则 →</a>' +
        '<button class="btn btn-sm btn-ghost" type="button" data-act="rules-import">' + icon('i-upload') + '导入规则 JSON</button>');
    }
    var lastRuns = lastRunsMap();
    return '<div class="table-wrap"><table class="table rule-table">' +
      '<thead><tr><th>编号</th><th>名称</th><th>触发</th><th>动作</th><th>上次执行</th><th>启用</th><th class="col-actions">操作</th></tr></thead><tbody>' +
      S.rules.map(function (rule, index) {
        return ruleRowHTML(rule, lastRuns[rule.id], index);
      }).join('') +
      '</tbody></table></div>';
  }

  function renderRulesPage() {
    render(
      pageHead('自动化', '定时、蓝牙事件、开机与手动触发的规则',
        '<button class="btn" type="button" data-act="rules-import">' + icon('i-upload') + '导入</button>' +
        '<button class="btn" type="button" data-act="rules-export">' + icon('i-download') + '导出</button>' +
        '<a class="btn btn-primary" href="#/rules/new">' + icon('i-plus') + '新建规则</a>') +
      '<div class="layout">' +
      cardHTML({
        cls: 'col-12', iconName: 'i-rules', title: '规则列表', flush: true,
        right: '<span class="card-sub" id="rules-stats">共 ' + S.ruleCounts.total + ' 条 · ' + S.ruleCounts.enabled + ' 条启用</span>',
        body: '<div id="rule-list-body">' + rulesListHTML() + '</div>'
      }) +
      '</div>'
    );
  }

  function updateRuleList() {
    patchById('rule-list-body', rulesListHTML());
    setText('rules-stats', '共 ' + S.ruleCounts.total + ' 条 · ' + S.ruleCounts.enabled + ' 条启用');
  }

  function scheduleRulesReload() {
    if (rulesReloadTimer) return;
    rulesReloadTimer = setTimeout(function () {
      rulesReloadTimer = null;
      loadRules().then(function () {
        if (S.route === 'rules' && !S.parts[1]) updateRuleList();
      }).catch(function () { /* 静默 */ });
    }, 900);
  }

  function syncRules(st) {
    if (!st) return;
    var counts = st.rules || {};
    setText('rules-stats', '共 ' + (Number(counts.total) || 0) + ' 条 · ' + (Number(counts.enabled) || 0) + ' 条启用');
    var changed = S.rulesLoaded &&
      (Number(counts.total) !== S.ruleCounts.total || Number(counts.enabled) !== S.ruleCounts.enabled);
    S.ruleCounts = { total: Number(counts.total) || 0, enabled: Number(counts.enabled) || 0 };
    if (changed) scheduleRulesReload();
  }

  function toggleRule(btn) {
    var id = btn.dataset.id;
    var rule = null;
    S.rules.forEach(function (r) { if (r.id === id) rule = r; });
    if (!rule) return;
    var next = !rule.enabled;
    btn.disabled = true;
    api('/api/rules/' + encodeURIComponent(id) + '/toggle', {
      method: 'POST', body: { enabled: next }, timeout: 15000
    }).then(function () {
      rule.enabled = next;
      toast('已' + (next ? '启用' : '停用') + '「' + (rule.name || '规则') + '」');
      updateRuleList();
      loadRules().catch(function () { /* 静默 */ });
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).then(function () {
      btn.disabled = false;
    });
  }

  function runRule(btn) {
    var id = btn.dataset.id;
    var rule = null;
    S.rules.forEach(function (r) { if (r.id === id) rule = r; });
    return busy(btn, function () {
      return api('/api/rules/' + encodeURIComponent(id) + '/run', { method: 'POST', body: {}, timeout: 15000 }).then(function (data) {
        toast('已提交执行：' + (rule && rule.name ? rule.name : '规则') + (data && data.job_id ? '（任务 ' + data.job_id + '）' : ''));
        setTimeout(function () {
          loadEvents().then(function () {
            if (S.route === 'rules' && !S.parts[1]) updateRuleList();
          }).catch(function () { /* 静默 */ });
        }, 2500);
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function deleteRule(btn) {
    var id = btn.dataset.id;
    var rule = null;
    S.rules.forEach(function (r) { if (r.id === id) rule = r; });
    return confirmDialog({
      title: '删除规则？',
      message: '将删除规则「' + (rule && rule.name ? rule.name : id) + '」，此操作不可撤销。',
      confirmText: '删除规则',
      danger: true
    }).then(function (ok) {
      if (!ok) return undefined;
      return busy(btn, function () {
        return api('/api/rules/' + encodeURIComponent(id), { method: 'DELETE', timeout: 15000 }).then(function () {
          toast('规则已删除');
          return loadRules();
        }).then(function () {
          updateRuleList();
        }).catch(function (err) {
          toast(err.message, 'danger');
        });
      });
    });
  }

  function exportRules(btn) {
    return busy(btn, function () {
      return api('/api/rules/export', { timeout: 20000 }).then(function (data) {
        var blob = new Blob([JSON.stringify(data || { version: 1, rules: [] }, null, 2)], { type: 'application/json' });
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        var stamp = fmtDateTime(new Date()).replace(/[: ]/g, '-');
        link.href = url;
        link.download = 'btspeaker-rules-' + stamp + '.json';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(url); }, 3000);
        toast('规则已导出为 JSON 文件');
      }).catch(function (err) {
        toast('导出失败：' + err.message, 'danger');
      });
    });
  }

  // 配置备份：GET /api/backup → {filename, content}，按服务端文件名下载 JSON。
  function exportBackup(btn) {
    return busy(btn, function () {
      return api('/api/backup', { timeout: 30000 }).then(function (data) {
        var payload = data || {};
        var content = payload.content !== undefined ? payload.content : payload;
        var filename = payload.filename ||
          ('btspeaker-backup-' + fmtDateTime(new Date()).replace(/[: ]/g, '-') + '.json');
        var blob = new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' });
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(url); }, 3000);
        toast('备份已下载');
      }).catch(function (err) {
        toast('备份导出失败：' + err.message, 'danger');
      });
    });
  }

  function openImportDialog() {
    var dialog = openModal(
      '<div class="modal-head"><span class="modal-ic">' + icon('i-upload') + '</span><h2 class="modal-title">导入规则</h2></div>' +
      '<p class="modal-text">支持本应用导出的 JSON（含 rules 数组）或规则数组。导入的规则会经过后端校验。</p>' +
      '<label class="field" for="import-file"><span>从文件选择</span><input type="file" id="import-file" accept="application/json,.json"></label>' +
      '<label class="field" for="import-text"><span>或粘贴 JSON</span><textarea id="import-text" rows="8" placeholder="粘贴导出的规则 JSON"></textarea></label>' +
      '<fieldset class="radio-row"><legend>导入方式</legend>' +
      '<label><input type="radio" name="import-mode" value="merge" checked> 合并（保留现有规则，同 ID 覆盖）</label>' +
      '<label><input type="radio" name="import-mode" value="replace"> 替换（清空现有规则后导入）</label></fieldset>' +
      '<div class="modal-error" id="import-error" hidden></div>' +
      '<div class="modal-actions">' +
      '<button class="btn" type="button" data-role="cancel">取消</button>' +
      '<button class="btn btn-primary" type="button" data-role="submit">开始导入</button>' +
      '</div>',
      { label: '导入规则' }
    );
    if (!dialog) return;
    var fileInput = dialog.modal.querySelector('#import-file');
    var textArea = dialog.modal.querySelector('#import-text');
    var errorEl = dialog.modal.querySelector('#import-error');

    function showError(message) {
      errorEl.hidden = false;
      errorEl.textContent = message;
    }
    function clearError() { errorEl.hidden = true; }

    fileInput.addEventListener('change', function () {
      var file = fileInput.files && fileInput.files[0];
      if (!file) return;
      if (file.size > 2 * 1024 * 1024) {
        showError('文件过大，规则 JSON 不能超过 2 MB。');
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        clearError();
        textArea.value = String(reader.result || '');
      };
      reader.onerror = function () { showError('读取文件失败，请重试。'); };
      reader.readAsText(file, 'utf-8');
    });
    textArea.addEventListener('input', clearError);

    dialog.modal.querySelector('[data-role="cancel"]').addEventListener('click', function () { dialog.close(); });
    var submit = dialog.modal.querySelector('[data-role="submit"]');
    submit.addEventListener('click', function () {
      var raw = textArea.value.trim();
      if (!raw) { showError('请选择文件或粘贴规则 JSON。'); return; }
      var parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (err) {
        showError('JSON 解析失败：' + err.message);
        return;
      }
      var rules = [];
      if (Array.isArray(parsed)) rules = parsed;
      else if (parsed && Array.isArray(parsed.rules)) rules = parsed.rules;
      else { showError('格式不正确：需要规则数组或包含 rules 数组的对象。'); return; }
      if (!rules.length) { showError('没有可导入的规则。'); return; }
      if (rules.length > 500) { showError('一次最多导入 500 条规则。'); return; }
      var modeEl = dialog.modal.querySelector('input[name="import-mode"]:checked');
      var mode = modeEl ? modeEl.value : 'merge';

      function doImport() {
        return busy(submit, function () {
          return api('/api/rules/import', { method: 'POST', body: { rules: rules, mode: mode }, timeout: 40000 }).then(function (data) {
            dialog.close();
            var count = data && data.imported != null ? data.imported : rules.length;
            toast('已导入 ' + count + ' 条规则');
            loadRules().then(function () {
              if (S.route === 'rules' && !S.parts[1]) updateRuleList();
            }).catch(function () { /* 静默 */ });
          }).catch(function (err) {
            showError(err.message);
          });
        });
      }

      if (mode === 'replace') {
        confirmDialog({
          title: '替换现有规则？',
          message: '替换模式会清空当前全部规则，然后导入这 ' + rules.length + ' 条规则。建议先导出备份。',
          confirmText: '清空并导入',
          danger: true
        }).then(function (ok) {
          if (!ok) return;
          clearError();
          doImport();
        });
      } else {
        clearError();
        doImport();
      }
    });
  }

  /* ---------------------------------------------------------------- 自动化：规则编辑器 */

  function blankRule() {
    return {
      name: '',
      enabled: true,
      cooldown: 0,
      trigger: { type: 'schedule', at: '07:30', days: [] },
      conditions: [],
      actions: [{ type: 'tts', text: '' }]
    };
  }

  function dayPickerHTML(selected) {
    var set = {};
    (selected || []).forEach(function (d) { set[Number(d)] = true; });
    return '<div class="day-picker">' + DAY_LABELS.map(function (label, index) {
      return '<label class="day"><input type="checkbox" value="' + index + '" data-days' +
        (set[index] ? ' checked' : '') + '><span>' + label + '</span></label>';
    }).join('') + '</div>';
  }

  function triggerBlocksHTML(trigger, type, mode) {
    var at = trigger.at || '07:30';
    var days = Array.isArray(trigger.days) ? trigger.days : [];
    var every = trigger.every_minutes != null ? trigger.every_minutes : 30;
    var btEvent = trigger.event || 'connected';
    var btDevice = trigger.device || '';
    var delay = trigger.delay != null ? trigger.delay : 5;
    return '<div data-trigger-block="schedule" class="' + (type === 'schedule' ? '' : 'is-hidden') + '">' +
      '<div class="segmented" role="radiogroup" aria-label="定时方式">' +
      '<label class="seg"><input type="radio" name="schedule-mode" value="at"' + (mode === 'at' ? ' checked' : '') + '><span>每天 / 指定星期</span></label>' +
      '<label class="seg"><input type="radio" name="schedule-mode" value="every"' + (mode === 'every' ? ' checked' : '') + '><span>每隔 N 分钟</span></label>' +
      '</div>' +
      '<div data-trigger-mode="at" class="' + (mode === 'at' ? '' : 'is-hidden') + '">' +
      '<div class="form-grid">' +
      '<label class="field"><span>触发时间</span><input id="trig-at" type="time" value="' + escAttr(at) + '"></label>' +
      '<div class="field full"><span class="label-text">重复星期（不勾选 = 每天）</span>' +
      '<div id="trig-at-days">' + dayPickerHTML(days) + '</div></div></div></div>' +
      '<div data-trigger-mode="every" class="' + (mode === 'every' ? '' : 'is-hidden') + '">' +
      '<label class="field field--sm"><span>间隔分钟数（1 - 1440）</span>' +
      '<input id="trig-every" type="number" min="1" max="1440" value="' + escAttr(every) + '"></label>' +
      '<p class="hint">' + icon('i-info') + '从服务启动后开始计时，错过的触发不会补执行。</p></div></div>' +

      '<div data-trigger-block="bluetooth" class="' + (type === 'bluetooth' ? '' : 'is-hidden') + '">' +
      '<div class="form-grid">' +
      '<label class="field"><span>蓝牙事件</span><select id="trig-bt-event">' +
      [['connected', '蓝牙连上'], ['disconnected', '蓝牙断开'], ['paired', '蓝牙配对']].map(function (item) {
        return '<option value="' + item[0] + '"' + (btEvent === item[0] ? ' selected' : '') + '>' + item[1] + '</option>';
      }).join('') + '</select></label>' +
      '<label class="field"><span>指定设备 MAC（留空 = 任意设备）</span>' +
      '<input id="trig-bt-device" type="text" list="device-addresses" value="' + escAttr(btDevice) + '" placeholder="AA:BB:CC:DD:EE:FF"></label>' +
      '</div></div>' +

      '<div data-trigger-block="boot" class="' + (type === 'boot' ? '' : 'is-hidden') + '">' +
      '<label class="field field--sm"><span>开机后延迟（秒）</span>' +
      '<input id="trig-boot-delay" type="number" min="0" max="3600" value="' + escAttr(delay) + '"></label>' +
      '<p class="hint">' + icon('i-info') + '服务启动后按延迟时间触发一次，用于开机欢迎语等场景。</p></div>' +

      '<div data-trigger-block="manual" class="' + (type === 'manual' ? '' : 'is-hidden') + '">' +
      '<p class="hint">' + icon('i-info') + '仅通过规则列表的「立即执行」按钮或外部 HTTP 钩子触发。</p></div>';
  }

  function condFieldsHTML(cond) {
    var type = cond.type || 'connected';
    if (type === 'time_between') {
      return '<label class="field"><span>开始时间</span><input type="time" data-cf="start" value="' +
        escAttr(cond.start || '08:00') + '"></label>' +
        '<label class="field"><span>结束时间</span><input type="time" data-cf="end" value="' +
        escAttr(cond.end || '22:00') + '"></label>';
    }
    if (type === 'weekday_in') {
      return '<div class="field"><span class="label-text">生效星期（全部满足才执行）</span>' +
        dayPickerHTML(cond.days || []) + '</div>';
    }
    var value = cond.value === false ? 'false' : 'true';
    return '<label class="field field-inline"><span>要求</span><select data-cf="value">' +
      '<option value="true"' + (value === 'true' ? ' selected' : '') + '>音箱已连接</option>' +
      '<option value="false"' + (value === 'false' ? ' selected' : '') + '>音箱未连接</option></select></label>';
  }

  function conditionRowHTML(cond) {
    var type = cond.type || 'connected';
    return '<div class="cond-row" data-cond>' +
      '<select class="cond-type" data-cond-type aria-label="条件类型">' +
      COND_TYPES.map(function (item) {
        return '<option value="' + item.value + '"' + (item.value === type ? ' selected' : '') + '>' + item.label + '</option>';
      }).join('') + '</select>' +
      '<div class="cond-fields">' + condFieldsHTML(cond) + '</div>' +
      '<button class="btn btn-icon btn-sm btn-ghost danger" type="button" data-act="del-condition" aria-label="删除条件">' +
      icon('i-trash') + '</button></div>';
  }

  function conditionRowsHTML(conditions) {
    if (!Array.isArray(conditions) || !conditions.length) return '';
    return conditions.map(conditionRowHTML).join('');
  }

  function actionFieldsHTML(action) {
    var a = action || {};
    var type = a.type || 'tts';
    var attr = function (name) { return 'data-cf="' + name + '"'; };
    var value = function (input) { return escAttr(input == null ? '' : input); };
    if (type === 'play') {
      return '<label class="field full"><span>播放源（绝对路径 / 声音库文件名 / 网络地址）</span>' +
        '<input type="text" ' + attr('source') + ' list="sound-sources" value="' + value(a.source) + '" placeholder="例如 chime.wav"></label>' +
        '<label class="field"><span>显示名称（可选）</span><input type="text" ' + attr('label') + ' value="' + value(a.label) + '" placeholder="欢迎提示音"></label>' +
        '<label class="field"><span>音量 0-100（留空用全局音量）</span><input type="number" min="0" max="100" ' + attr('volume') + ' value="' + value(a.volume) + '"></label>' +
        '<label class="check-row"><input type="checkbox" ' + attr('loop') + (a.loop ? ' checked' : '') + '><span>循环播放直到被停止</span></label>';
    }
    if (type === 'tts') {
      return '<label class="field full"><span>播报文本（支持 {time} {date} {weekday} {bt_name} 等模板变量）</span>' +
        '<textarea rows="2" ' + attr('text') + ' placeholder="现在是 {time}，记得休息一下。">' + esc(a.text || '') + '</textarea></label>' +
        '<label class="field"><span>语音（可选，留空用全局配置）</span><input type="text" ' + attr('voice') + ' value="' + value(a.voice) + '" placeholder="zh"></label>' +
        '<label class="field"><span>音量 0-100（可选）</span><input type="number" min="0" max="100" ' + attr('volume') + ' value="' + value(a.volume) + '"></label>';
    }
    if (type === 'ai') {
      return '<label class="field full"><span>提示词 PROMPT（支持模板变量，如 {time} {date} {weekday}）</span>' +
        '<textarea rows="3" ' + attr('prompt') + ' placeholder="用一句简短的中文播报：今天是 {date}，{weekday}，{time}。">' + esc(a.prompt || '') + '</textarea></label>' +
        '<label class="field full"><span>系统提示词（可选，留空用全局 AI 设置）</span>' +
        '<textarea rows="2" ' + attr('system') + ' placeholder="你是蓝音管家，用简洁口语播报">' + esc(a.system || '') + '</textarea></label>' +
        '<label class="field"><span>模型（可选）</span><input type="text" ' + attr('model') + ' value="' + value(a.model) + '" placeholder="留空用全局配置"></label>' +
        '<label class="field"><span>音量 0-100（可选）</span><input type="number" min="0" max="100" ' + attr('volume') + ' value="' + value(a.volume != null ? a.volume : '') + '"></label>' +
        '<p class="hint">' + icon('i-info') + '需要先在「设置 → AI 播报」里配置接口地址、API Key 并启用。</p>';
    }
    if (type === 'volume') {
      return '<label class="field"><span>目标音量 0-100</span><input type="number" min="0" max="100" ' + attr('level') + ' value="' + value(a.level != null ? a.level : 60) + '"></label>';
    }
    if (type === 'connect') {
      return '<label class="field full"><span>目标音箱 MAC（留空 = 默认音箱）</span>' +
        '<input type="text" ' + attr('address') + ' list="device-addresses" value="' + value(a.address) + '" placeholder="AA:BB:CC:DD:EE:FF"></label>';
    }
    if (type === 'disconnect') {
      return '<p class="hint">' + icon('i-info') + '断开当前连接的音箱，无需额外参数。</p>';
    }
    if (type === 'http') {
      return '<label class="field full"><span>请求地址（支持模板变量）</span>' +
        '<input type="text" ' + attr('url') + ' value="' + value(a.url) + '" placeholder="http://192.168.1.10:8080/hook?t={time}"></label>' +
        '<label class="field"><span>请求方法</span><select ' + attr('method') + '>' +
        ['GET', 'POST', 'PUT', 'DELETE'].map(function (m) {
          return '<option value="' + m + '"' + ((a.method || 'GET') === m ? ' selected' : '') + '>' + m + '</option>';
        }).join('') + '</select></label>' +
        '<label class="field"><span>超时（秒）</span><input type="number" min="1" max="120" ' + attr('timeout') + ' value="' + value(a.timeout != null ? a.timeout : 10) + '"></label>' +
        '<label class="field full"><span>请求头（JSON 对象，可选）</span><textarea rows="2" ' + attr('headers') + ' placeholder=\'{"Content-Type": "application/json"}\'>' +
        esc(a.headers && Object.keys(a.headers).length ? JSON.stringify(a.headers, null, 2) : '') + '</textarea></label>' +
        '<label class="field full"><span>请求体（可选，支持模板变量）</span><textarea rows="2" ' + attr('body') + '>' + esc(a.body || '') + '</textarea></label>';
    }
    if (type === 'wait') {
      return '<label class="field"><span>等待秒数（0 - 300）</span><input type="number" min="0" max="300" ' + attr('seconds') + ' value="' + value(a.seconds != null ? a.seconds : 2) + '"></label>';
    }
    if (type === 'log') {
      return '<label class="field full"><span>日志内容（支持模板变量）</span>' +
        '<input type="text" ' + attr('message') + ' value="' + value(a.message) + '" placeholder="规则 {rule_name} 已执行"></label>';
    }
    if (type === 'command') {
      return '<label class="field full"><span>要执行的命令</span>' +
        '<input type="text" ' + attr('command') + ' value="' + value(a.command) + '" placeholder="sh /root/scripts/greeting.sh"></label>' +
        '<label class="field"><span>超时（秒）</span><input type="number" min="1" max="600" ' + attr('timeout') + ' value="' + value(a.timeout != null ? a.timeout : 20) + '"></label>' +
        '<p class="hint hint-warn">' + icon('i-alert') + '需要在设置页开启「允许执行系统命令」，并遵守命令白名单前缀。</p>';
    }
    return '<p class="hint">未知动作类型。</p>';
  }

  function actionRowHTML(action, index) {
    var type = action.type || 'tts';
    var displayIndex = (index + 1 < 10 ? '0' : '') + (index + 1);
    return '<div class="action-row" data-action>' +
      '<div class="action-head">' +
      '<span class="action-num" data-action-num>' + displayIndex + '</span>' +
      '<select class="action-type" data-action-type aria-label="动作类型">' +
      ACTION_TYPES.map(function (item) {
        return '<option value="' + item.value + '"' + (item.value === type ? ' selected' : '') + '>' + item.label + '</option>';
      }).join('') + '</select>' +
      '<div class="action-tools">' +
      '<button class="btn btn-icon btn-sm btn-ghost" type="button" data-act="move-action-up" aria-label="上移动作">' + icon('i-up') + '</button>' +
      '<button class="btn btn-icon btn-sm btn-ghost" type="button" data-act="move-action-down" aria-label="下移动作">' + icon('i-down') + '</button>' +
      '<button class="btn btn-icon btn-sm btn-ghost danger" type="button" data-act="del-action" aria-label="删除动作">' + icon('i-trash') + '</button>' +
      '</div></div>' +
      '<div class="action-fields">' + actionFieldsHTML(action) + '</div></div>';
  }

  function actionRowsHTML(actions) {
    if (!Array.isArray(actions) || !actions.length) actions = [{ type: 'tts', text: '' }];
    return actions.map(actionRowHTML).join('');
  }

  function renumberActions() {
    $$('#action-list [data-action]').forEach(function (row, index) {
      var num = row.querySelector('[data-action-num]');
      if (num) num.textContent = (index + 1 < 10 ? '0' : '') + (index + 1);
    });
  }

  function soundDatalistOptions() {
    var seen = {};
    return S.library.map(function (item) {
      var value = item.name || item.path || '';
      if (!value || seen[value]) return '';
      seen[value] = true;
      return '<option value="' + escAttr(value) + '"></option>';
    }).join('');
  }

  function deviceDatalistOptions() {
    var seen = {};
    return deviceItems().map(function (dev) {
      if (!dev.address || seen[dev.address]) return '';
      seen[dev.address] = true;
      return '<option value="' + escAttr(dev.address) + '">' + esc(deviceLabel(dev)) + '</option>';
    }).join('');
  }

  function renderRuleEditor(rule, isEdit) {
    var trigger = rule.trigger || { type: 'schedule', at: '07:30', days: [] };
    var conditions = Array.isArray(rule.conditions) ? rule.conditions : [];
    var actions = Array.isArray(rule.actions) && rule.actions.length ? rule.actions : [{ type: 'tts', text: '' }];
    var triggerType = ['schedule', 'bluetooth', 'boot', 'manual'].indexOf(trigger.type) >= 0 ? trigger.type : 'schedule';
    var scheduleMode = trigger.every_minutes != null ? 'every' : 'at';
    var title = isEdit ? '编辑规则' : '新建规则';

    render(
      '<form id="rule-form" novalidate>' +
      pageHead(title, '设置触发器、前置条件与按顺序执行的动作',
        '<a class="btn" href="#/rules">' + icon('i-x') + '返回列表</a>') +
      '<div class="editor-grid">' +

      '<div class="col-7 stack">' +
      '<section class="card"><header class="card-head"><div class="card-title">' + icon('i-rules') +
      '<h2>基本信息</h2></div><div class="card-right"><span class="card-sub">01 / BASIC</span></div></header>' +
      '<div class="card-body">' +
      '<label class="field"><span>规则名称 RG-NAME（1-64 个字符）</span>' +
      '<input id="rule-name" type="text" maxlength="64" value="' + escAttr(rule.name || '') + '" placeholder="例如：早安播报"></label>' +
      '<div class="form-grid">' +
      '<label class="field"><span>冷却时间 COOLDOWN（秒，0 = 不限制）</span>' +
      '<input id="rule-cooldown" type="number" min="0" max="86400" value="' + escAttr(rule.cooldown || 0) + '"></label>' +
      '<label class="check-row" style="align-self:end"><input id="rule-enabled" type="checkbox"' +
      (rule.enabled !== false ? ' checked' : '') + '><span>启用这条规则</span></label>' +
      '</div>' +
      '<p class="hint">' + icon('i-info') + '冷却时间用于避免蓝牙事件频繁触发，例如设为 300 表示 5 分钟内只执行一次。</p>' +
      '</div></section>' +

      '<section class="card"><header class="card-head"><div class="card-title">' + icon('i-clock') +
      '<h2>触发器</h2></div><div class="card-right"><span class="card-sub">02 / TRIGGER</span></div></header>' +
      '<div class="card-body"><div class="stack-sm"><div class="segmented" role="radiogroup" aria-label="触发器类型">' +
      TRIGGER_TYPES.map(function (item) {
        return '<label class="seg"><input type="radio" name="trigger-type" value="' + item.value + '"' +
          (triggerType === item.value ? ' checked' : '') + '><span>' + item.label + '</span></label>';
      }).join('') + '</div>' +
      '<div id="trigger-fields">' + triggerBlocksHTML(trigger, triggerType, scheduleMode) + '</div>' +
      '</div></div></section>' +

      '<section class="card"><header class="card-head"><div class="card-title">' + icon('i-list') +
      '<h2>条件</h2></div><div class="card-right"><span class="card-sub">03 / CONDITIONS · 全部满足才执行</span></div></header>' +
      '<div class="card-body"><div id="cond-list" class="cond-list">' + conditionRowsHTML(conditions) + '</div>' +
      '<p class="hint" id="cond-empty"' + (conditions.length ? ' hidden' : '') + '>' + icon('i-info') +
      '没有条件：规则触发后会直接执行动作。可以添加「需已连接」「时间段」「星期」条件。</p>' +
      '<div class="add-bar"><select id="cond-add-type" aria-label="要添加的条件类型">' +
      COND_TYPES.map(function (item) { return '<option value="' + item.value + '">' + item.label + '</option>'; }).join('') +
      '</select><button class="btn btn-sm" type="button" data-act="add-condition">' + icon('i-plus') + '添加条件</button></div>' +
      '</div></section>' +
      '</div>' +

      '<div class="col-5 col--sticky">' +
      '<section class="card"><header class="card-head"><div class="card-title">' + icon('i-zap') +
      '<h2>动作编排</h2></div><div class="card-right"><span class="card-sub">04 / ACTIONS</span></div></header>' +
      '<div class="card-body"><p class="hint">' + icon('i-info') + '按顺序执行，单条失败不中断后续。</p>' +
      '<div id="action-list" class="action-list">' + actionRowsHTML(actions) + '</div>' +
      '<div class="add-bar"><select id="action-add-type" aria-label="要添加的动作类型">' +
      ACTION_TYPES.map(function (item) { return '<option value="' + item.value + '">' + item.label + '</option>'; }).join('') +
      '</select><button class="btn btn-sm" type="button" data-act="add-action">' + icon('i-plus') + '添加动作</button></div>' +
      '</div></section>' +
      '</div>' +

      '</div>' +
      '<div class="editor-bar"><p class="hint">' + icon('i-info') +
      '保存后立即生效，调度器会自动重新加载规则。</p>' +
      '<a class="btn" href="#/rules">取消</a>' +
      '<button class="btn btn-primary" type="submit">' + icon('i-check') + (isEdit ? '保存修改' : '创建规则') + '</button></div>' +
      '<datalist id="sound-sources">' + soundDatalistOptions() + '</datalist>' +
      '<datalist id="device-addresses">' + deviceDatalistOptions() + '</datalist>' +
      '</form>'
    );
    updateTriggerVisibility();
    renumberActions();
  }

  function updateTriggerVisibility() {
    var typeEl = $('input[name="trigger-type"]:checked');
    var type = typeEl ? typeEl.value : 'schedule';
    $$('[data-trigger-block]').forEach(function (block) {
      block.classList.toggle('is-hidden', block.getAttribute('data-trigger-block') !== type);
    });
    var modeEl = $('input[name="schedule-mode"]:checked');
    var mode = modeEl ? modeEl.value : 'at';
    $$('[data-trigger-mode]').forEach(function (block) {
      block.classList.toggle('is-hidden', block.getAttribute('data-trigger-mode') !== mode);
    });
  }

  function refreshCondEmpty() {
    var hint = document.getElementById('cond-empty');
    var list = document.getElementById('cond-list');
    if (!hint || !list) return;
    hint.hidden = list.querySelectorAll('[data-cond]').length > 0;
  }

  function addConditionRow() {
    var list = document.getElementById('cond-list');
    var select = document.getElementById('cond-add-type');
    if (!list) return;
    var type = select ? select.value : 'connected';
    list.insertAdjacentHTML('beforeend', conditionRowHTML({ type: type }));
    refreshCondEmpty();
  }

  function addActionRow() {
    var list = document.getElementById('action-list');
    var select = document.getElementById('action-add-type');
    if (!list) return;
    var type = select ? select.value : 'tts';
    var row = document.createElement('div');
    row.innerHTML = actionRowHTML({ type: type }, list.children.length);
    list.appendChild(row.firstElementChild);
    renumberActions();
  }

  function moveAction(btn, direction) {
    var row = btn.closest('[data-action]');
    if (!row || !row.parentNode) return;
    if (direction < 0 && row.previousElementSibling) {
      row.parentNode.insertBefore(row, row.previousElementSibling);
    } else if (direction > 0 && row.nextElementSibling) {
      row.parentNode.insertBefore(row.nextElementSibling, row);
    }
    renumberActions();
  }

  function collectCondition(row) {
    var typeEl = row.querySelector('[data-cond-type]');
    var type = typeEl ? typeEl.value : 'connected';
    function field(name) { return row.querySelector('[data-cf="' + name + '"]'); }
    if (type === 'time_between') {
      var start = field('start');
      var end = field('end');
      if (!start || !end || !start.value || !end.value) return { error: '时间段条件缺少开始或结束时间' };
      return { value: { type: 'time_between', start: start.value, end: end.value } };
    }
    if (type === 'weekday_in') {
      var days = $$('[data-days]:checked', row).map(function (input) { return parseInt(input.value, 10); })
        .filter(function (n) { return n >= 0 && n <= 6; });
      if (!days.length) return { error: '星期条件至少要选择一天' };
      return { value: { type: 'weekday_in', days: days } };
    }
    var valueEl = field('value');
    return { value: { type: 'connected', value: !valueEl || valueEl.value !== 'false' } };
  }

  function collectAction(row) {
    var typeEl = row.querySelector('[data-action-type]');
    var type = typeEl ? typeEl.value : 'tts';
    function field(name) { return row.querySelector('[data-cf="' + name + '"]'); }
    function text(name) {
      var el = field(name);
      return el ? String(el.value == null ? '' : el.value).trim() : '';
    }
    function number(name) {
      var raw = text(name);
      if (raw === '') return null;
      var n = parseInt(raw, 10);
      return Number.isFinite(n) ? n : NaN;
    }
    function checked(name) {
      var el = field(name);
      return !!(el && el.checked);
    }
    if (type === 'play') {
      var source = text('source');
      if (!source) return { error: '缺少播放源' };
      var play = { type: 'play', source: source };
      if (text('label')) play.label = text('label');
      var volume = number('volume');
      if (volume !== null) {
        if (isNaN(volume) || volume < 0 || volume > 100) return { error: '播放音量需要在 0-100 之间' };
        play.volume = volume;
      }
      if (checked('loop')) play.loop = true;
      return { value: play };
    }
    if (type === 'tts') {
      var ttsText = text('text');
      if (!ttsText) return { error: '缺少播报文本' };
      var tts = { type: 'tts', text: ttsText };
      if (text('voice')) tts.voice = text('voice');
      var ttsVolume = number('volume');
      if (ttsVolume !== null) {
        if (isNaN(ttsVolume) || ttsVolume < 0 || ttsVolume > 100) return { error: '播报音量需要在 0-100 之间' };
        tts.volume = ttsVolume;
      }
      return { value: tts };
    }
    if (type === 'ai') {
      var aiPrompt = text('prompt');
      if (!aiPrompt) return { error: 'AI 播报缺少提示词' };
      var aiAction = { type: 'ai', prompt: aiPrompt };
      if (text('system')) aiAction.system = text('system');
      if (text('model')) aiAction.model = text('model');
      var aiVolume = number('volume');
      if (aiVolume !== null) {
        if (isNaN(aiVolume) || aiVolume < 0 || aiVolume > 100) return { error: 'AI 播报音量需要在 0-100 之间' };
        aiAction.volume = aiVolume;
      }
      return { value: aiAction };
    }
    if (type === 'volume') {
      var level = number('level');
      if (level === null || isNaN(level) || level < 0 || level > 100) return { error: '目标音量需要在 0-100 之间' };
      return { value: { type: 'volume', level: level } };
    }
    if (type === 'connect') {
      var address = text('address');
      return { value: address ? { type: 'connect', address: address } : { type: 'connect' } };
    }
    if (type === 'disconnect') return { value: { type: 'disconnect' } };
    if (type === 'http') {
      var url = text('url');
      if (!url) return { error: '缺少请求地址' };
      if (!/^https?:\/\//i.test(url)) return { error: '请求地址需要以 http:// 或 https:// 开头' };
      var methodEl = field('method');
      var httpAction = { type: 'http', url: url, method: methodEl ? methodEl.value : 'GET' };
      var headersRaw = text('headers');
      if (headersRaw) {
        var headersObj;
        try { headersObj = JSON.parse(headersRaw); } catch (e) { return { error: '请求头不是合法 JSON' }; }
        if (!headersObj || typeof headersObj !== 'object' || Array.isArray(headersObj)) {
          return { error: '请求头需要是 JSON 对象' };
        }
        httpAction.headers = headersObj;
      }
      var body = field('body');
      if (body && body.value.trim()) httpAction.body = body.value;
      var timeoutValue = number('timeout');
      if (timeoutValue !== null && !isNaN(timeoutValue)) httpAction.timeout = clampInt(timeoutValue, 1, 120);
      return { value: httpAction };
    }
    if (type === 'wait') {
      var seconds = number('seconds');
      if (seconds === null || isNaN(seconds) || seconds < 0 || seconds > 300) return { error: '等待秒数需要在 0-300 之间' };
      return { value: { type: 'wait', seconds: seconds } };
    }
    if (type === 'log') {
      var message = text('message');
      if (!message) return { error: '缺少日志内容' };
      return { value: { type: 'log', message: message } };
    }
    if (type === 'command') {
      var command = text('command');
      if (!command) return { error: '缺少要执行的命令' };
      var commandTimeout = number('timeout');
      var commandAction = { type: 'command', command: command };
      if (commandTimeout !== null && !isNaN(commandTimeout)) commandAction.timeout = clampInt(commandTimeout, 1, 600);
      return { value: commandAction };
    }
    return { error: '未知动作类型' };
  }

  function collectTrigger() {
    var typeEl = $('input[name="trigger-type"]:checked');
    var type = typeEl ? typeEl.value : 'schedule';
    if (type === 'schedule') {
      var modeEl = $('input[name="schedule-mode"]:checked');
      var mode = modeEl ? modeEl.value : 'at';
      if (mode === 'every') {
        var everyEl = document.getElementById('trig-every');
        var every = everyEl ? parseInt(everyEl.value, 10) : NaN;
        if (!Number.isFinite(every) || every < 1 || every > 1440) {
          return { error: { el: everyEl, msg: '间隔分钟数需要在 1 - 1440 之间' } };
        }
        return { value: { type: 'schedule', every_minutes: every } };
      }
      var atEl = document.getElementById('trig-at');
      var at = atEl ? atEl.value : '';
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(at)) {
        return { error: { el: atEl, msg: '请填写合法的触发时间（HH:MM）' } };
      }
      var days = $$('#trig-at-days [data-days]:checked').map(function (input) { return parseInt(input.value, 10); })
        .filter(function (n) { return n >= 0 && n <= 6; });
      return { value: { type: 'schedule', at: at, days: days } };
    }
    if (type === 'bluetooth') {
      var eventEl = document.getElementById('trig-bt-event');
      var deviceEl = document.getElementById('trig-bt-device');
      return {
        value: {
          type: 'bluetooth',
          event: eventEl ? eventEl.value : 'connected',
          device: deviceEl ? deviceEl.value.trim() : ''
        }
      };
    }
    if (type === 'boot') {
      var delayEl = document.getElementById('trig-boot-delay');
      var delay = delayEl ? parseInt(delayEl.value, 10) : NaN;
      if (!Number.isFinite(delay) || delay < 0 || delay > 3600) {
        return { error: { el: delayEl, msg: '开机延迟需要在 0 - 3600 秒之间' } };
      }
      return { value: { type: 'boot', delay: delay } };
    }
    return { value: { type: 'manual' } };
  }

  function collectRuleForm() {
    var errors = [];
    var nameEl = document.getElementById('rule-name');
    var name = nameEl ? nameEl.value.trim() : '';
    if (!name) errors.push({ el: nameEl, msg: '请填写规则名称' });
    else if (name.length > 64) errors.push({ el: nameEl, msg: '规则名称不能超过 64 个字符' });

    var enabledEl = document.getElementById('rule-enabled');
    var cooldownEl = document.getElementById('rule-cooldown');
    var cooldown = cooldownEl ? clampInt(cooldownEl.value, 0, 86400) : 0;

    var triggerResult = collectTrigger();
    if (triggerResult.error) errors.push(triggerResult.error);

    var conditions = [];
    $$('#cond-list [data-cond]').forEach(function (row, index) {
      var result = collectCondition(row);
      if (result.error) errors.push({ el: row, msg: '第 ' + (index + 1) + ' 个条件：' + result.error });
      else conditions.push(result.value);
    });

    var actions = [];
    $$('#action-list [data-action]').forEach(function (row, index) {
      var result = collectAction(row);
      if (result.error) errors.push({ el: row, msg: '第 ' + (index + 1) + ' 个动作：' + result.error });
      else actions.push(result.value);
    });
    if (!actions.length) {
      var list = document.getElementById('action-list');
      errors.push({ el: list, msg: '至少需要添加一个动作' });
    }

    return {
      rule: {
        name: name,
        enabled: !!(enabledEl && enabledEl.checked),
        cooldown: cooldown,
        trigger: triggerResult.value,
        conditions: conditions,
        actions: actions
      },
      errors: errors
    };
  }

  function saveRuleForm(form) {
    var collected = collectRuleForm();
    if (collected.errors.length) {
      var first = collected.errors[0];
      if (first.el) {
        first.el.classList.add('is-invalid');
        try { first.el.focus(); } catch (e) { /* 忽略 */ }
      }
      toast(first.msg, 'danger');
      return;
    }
    var submit = form.querySelector('[type="submit"]');
    var editingId = S.editorId || '';
    return busy(submit, function () {
      var request = editingId
        ? api('/api/rules/' + encodeURIComponent(editingId), { method: 'PUT', body: collected.rule, timeout: 20000 })
        : api('/api/rules', { method: 'POST', body: collected.rule, timeout: 20000 });
      return request.then(function () {
        toast(editingId ? '规则已保存' : '规则已创建');
        S.editorId = '';
        return loadRules().catch(function () { return null; });
      }).then(function () {
        location.hash = '#/rules';
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function mountRuleEditor(parsed, seq) {
    var isEdit = parsed.parts[1] === 'edit';
    var id = isEdit ? decodeURIComponent(parsed.parts[2] || '') : '';
    var title = isEdit ? '编辑规则' : '新建规则';
    document.title = title + ' · 蓝音管家 BTSpeaker';
    renderPageSkeleton(title, '设置触发器、前置条件与按顺序执行的动作',
      '<a class="btn" href="#/rules">' + icon('i-x') + '返回列表</a>');
    return ensureStatus().then(function (st) {
      if (seq !== S.navSeq) return;
      if (!st) {
        S.routeOffline = true;
        renderOffline(title, '无法读取运行状态');
        return;
      }
      S.routeOffline = false;
      var chain = Promise.resolve(null);
      if (!isEdit) chain = Promise.resolve(blankRule());
      else {
        chain = loadRules().catch(function () { return null; }).then(function () {
          var found = null;
          S.rules.forEach(function (r) { if (r.id === id) found = r; });
          if (found) return found;
          return api('/api/rules/' + encodeURIComponent(id), { timeout: 12000 });
        });
      }
      return chain.then(function (rule) {
        if (seq !== S.navSeq) return null;
        if (!rule) {
          render(pageHead(title, '规则不存在') +
            '<div class="layout"><div class="col-12"><div class="card">' +
            emptyHTML('i-alert', '规则不存在', '这条规则可能已经被删除，请返回列表查看最新规则。',
              '<a class="btn" href="#/rules">返回规则列表</a>') +
            '</div></div></div>');
          return null;
        }
        return loadLibrary().catch(function () { return null; }).then(function () {
          if (seq !== S.navSeq) return;
          S.editorId = isEdit ? id : '';
          renderRuleEditor(rule, isEdit);
        });
      });
    });
  }

  var rulesRoute = {
    mount: function (parsed, seq) {
      if (parsed.parts[1] === 'new' || parsed.parts[1] === 'edit') {
        return mountRuleEditor(parsed, seq);
      }
      renderPageSkeleton('自动化', '定时、蓝牙事件、开机与手动触发的规则',
        '<button class="btn" type="button" data-act="rules-import">' + icon('i-upload') + '导入</button>' +
        '<button class="btn" type="button" data-act="rules-export">' + icon('i-download') + '导出</button>' +
        '<a class="btn btn-primary" href="#/rules/new">' + icon('i-plus') + '新建规则</a>');
      return ensureStatus().then(function (st) {
        if (seq !== S.navSeq) return;
        if (!st) {
          S.routeOffline = true;
          renderOffline('自动化', '无法读取规则与运行状态');
          return;
        }
        S.routeOffline = false;
        return Promise.all([
          loadRules().catch(function () { return null; }),
          loadEvents().catch(function () { return null; })
        ]).then(function () {
          if (seq !== S.navSeq) return;
          renderRulesPage();
          var timer = setInterval(function () {
            if (S.route !== 'rules' || S.parts[1]) return;
            loadEvents().then(function () {
              if (!S.parts[1]) updateRuleList();
            }).catch(function () { /* 静默 */ });
          }, 15000);
          routeTimers.push(timer);
        });
      });
    },
    sync: syncRules
  };

  /* ---------------------------------------------------------------- 设置页 */

  function systemRowsHTML() {
    var sys = S.system;
    if (!sys) return '<p class="hint">' + icon('i-alert') + '系统信息加载失败，请点击「刷新」重试。</p>';
    var pairs = [
      ['产品', '蓝音管家 BTSpeaker'],
      ['项目', sys.project || '—'],
      ['版本', sys.version || '—'],
      ['Python', sys.python || '—'],
      ['平台', sys.platform || '—'],
      ['主机名', sys.hostname || '—'],
      ['启动时间', fmtDateTime(sys.started_at)],
      ['运行时长', fmtUptime(sys.uptime)],
      ['数据目录', sys.data_dir || '—', true]
    ];
    return kvRows(pairs);
  }

  function diagBodyHTML() {
    var diag = S.diag;
    var checks = diag && Array.isArray(diag.checks) ? diag.checks : [];
    if (!checks.length) {
      return '<p class="hint">' + icon('i-info') + '还没有诊断结果，点击「重新检测」开始检查蓝牙、播放器与 TTS 配置。</p>';
    }
    var failed = checks.filter(function (c) { return !c.ok; }).length;
    var html = '<div class="diag-list">' + checks.map(function (check) {
      var ok = !!check.ok;
      return '<div class="diag-row">' +
        '<span class="diag-sq' + (ok ? '' : ' fail') + '" aria-hidden="true"></span>' +
        '<div class="diag-main">' +
        '<p class="diag-name">' + esc(check.name || '检查项') + '</p>' +
        (check.detail ? '<p class="diag-detail">' + esc(check.detail) + '</p>' : '') +
        (!ok && check.hint ? '<p class="diag-hint">' + esc(check.hint) + '</p>' : '') +
        '</div>' +
        '<span class="tag ' + (ok ? 'tag--ghost' : 'tag--strong') + '">' + (ok ? '通过' : '失败') + '</span></div>';
    }).join('') + '</div>';
    html += '<p class="hint">' + icon('i-info') + checks.length + ' 项检查，' +
      (failed ? failed + ' 项失败，请按提示处理后重新检测。' : '全部通过。') + '</p>';
    return html;
  }

  function diagPillHTML() {
    var diag = S.diag;
    if (!diag || !Array.isArray(diag.checks) || !diag.checks.length) return pill('未检测', 'muted');
    var failed = diag.checks.filter(function (c) { return !c.ok; }).length;
    return failed ? pill(failed + ' 项失败', 'danger') : pill('全部通过', 'ok');
  }

  function renderSettingsPage(config) {
    var st = S.status || {};
    var cfg = config || null;
    var tts = (cfg && cfg.tts) || {};
    var ai = (cfg && cfg.ai) || {};
    var player = (cfg && cfg.player) || {};
    var bluetooth = (cfg && cfg.bluetooth) || {};
    var security = (cfg && cfg.security) || {};
    var ttsStatus = st.tts || {};
    var aiProbe = st.ai || {};
    var playerProbe = (st.player && st.player.probe) || {};
    var sim = !!(st.sim || (S.system && S.system.sim));
    var headers = tts.headers && Object.keys(tts.headers).length ? JSON.stringify(tts.headers, null, 2) : '';
    var allowlist = Array.isArray(security.command_allowlist) ? security.command_allowlist.join('\n') : '';
    var securityHint = security.api_token
      ? '已设置（留空表示不修改）'
      : '未设置（留空表示不启用鉴权，局域网使用建议设置）';

    var formsHTML = '';
    if (!cfg) {
      formsHTML = '<div class="layout"><div class="col-12"><div class="card">' +
        emptyHTML('i-alert', '设置读取失败',
          '无法从后端读取配置，可能是服务未启动或令牌无效。请确认服务状态后重新加载。',
          '<button class="btn" type="button" data-act="refresh">' + icon('i-refresh') + '重新加载</button>') +
        '</div></div></div>';
    } else {
      formsHTML = '<div class="settings-grid"><div class="col-7 stack">' +
        /* 系统信息 */
        cardHTML({
          iconName: 'i-info', title: '系统信息',
          right: '<button class="btn btn-sm btn-ghost" type="button" data-act="reload-system">' + icon('i-refresh') + '刷新</button>',
          body: '<div id="sysinfo-body">' + systemRowsHTML() + '</div>' +
            '<div class="set-form-actions">' +
            '<button class="btn" type="button" data-act="backup-export">' + icon('i-download') + '导出配置备份</button>' +
            '<span class="helper">包含 config.json 与全部规则</span>' +
            '</div>'
        }) +
        /* TTS */
        cardHTML({
          iconName: 'i-music', title: 'TTS 语音播报',
          right: ttsStatus.backend ? pill('后端：' + ttsStatus.backend + (ttsStatus.ready === false ? '（未就绪）' : ''), ttsStatus.ready === false ? 'warn' : 'ok') : '',
          body: '<form data-settings-section="tts" novalidate>' +
            '<div class="form-grid">' +
            '<label class="field"><span>后端</span><select id="set-tts-backend">' +
            [['auto', '自动（优先 HTTP，其次 espeak）'], ['http', 'HTTP 服务'], ['espeak', 'espeak 离线'], ['mock', '模拟（不发声）']].map(function (item) {
              return '<option value="' + item[0] + '"' + ((tts.backend || 'auto') === item[0] ? ' selected' : '') + '>' + item[1] + '</option>';
            }).join('') + '</select></label>' +
            '<label class="field"><span>语音 / 语言（voice）</span><input id="set-tts-voice" type="text" value="' + escAttr(tts.voice || 'zh') + '" placeholder="zh"></label>' +
            '<label class="field full"><span>TTS 服务地址（{text} 会被替换为播报文本，{lang} 为语音）</span>' +
            '<input id="set-tts-url" type="text" value="' + escAttr(tts.url || '') + '" placeholder="http://192.168.1.2:8080/tts?text={text}&lang={lang}"></label>' +
            '<label class="field"><span>请求方法</span><select id="set-tts-method">' +
            ['GET', 'POST'].map(function (m) {
              return '<option value="' + m + '"' + ((tts.method || 'GET') === m ? ' selected' : '') + '>' + m + '</option>';
            }).join('') + '</select></label>' +
            '<label class="field"><span>音频格式（无法识别响应类型时使用）</span><select id="set-tts-format">' +
            ['mp3', 'wav', 'ogg'].map(function (f) {
              return '<option value="' + f + '"' + ((tts.format || 'mp3') === f ? ' selected' : '') + '>' + f + '</option>';
            }).join('') + '</select></label>' +
            '<label class="field full"><span>请求头（JSON 对象，可选）</span>' +
            '<textarea id="set-tts-headers" rows="3" placeholder=\'{"Authorization": "Bearer xxx"}\'>' + esc(headers) + '</textarea></label>' +
            '<label class="field full"><span>POST 请求体模板（可选，同样支持模板变量）</span>' +
            '<textarea id="set-tts-body" rows="3" placeholder="text={text}&amp;lang={lang}">' + esc(tts.body_template || '') + '</textarea></label>' +
            '<label class="field"><span>语速</span><input id="set-tts-speed" type="number" min="50" max="400" value="' + escAttr(tts.speed != null ? tts.speed : 150) + '"></label>' +
            '<label class="field"><span>超时（秒）</span><input id="set-tts-timeout" type="number" min="3" max="300" value="' + escAttr(tts.timeout != null ? tts.timeout : 20) + '"></label>' +
            '<label class="field"><span>espeak 命令</span><input id="set-tts-espeak" type="text" value="' + escAttr(tts.espeak || 'espeak') + '"></label>' +
            '</div>' +
            '<div class="inline-test"><input id="set-tts-test-text" type="text" placeholder="输入测试文本，例如：测试播报" aria-label="测试播报文本">' +
            '<button class="btn" type="button" data-act="tts-test">' + icon('i-play') + '测试播报</button>' +
            '<button class="btn" type="submit">' + icon('i-check') + '保存 TTS 设置</button></div>' +
            '</form>'
        }) +
        /* AI 播报 */
        cardHTML({
          iconName: 'i-zap', title: 'AI 播报',
          right: aiProbe.ready ? pill('AI 已就绪', 'ok') : pill('未配置', 'warn'),
          body: '<form data-settings-section="ai" novalidate><div class="form-grid">' +
            '<label class="check-row full"><input id="set-ai-enabled" type="checkbox"' + (ai.enabled ? ' checked' : '') + '><span>启用 AI 播报（规则动作与测试都需要开启）</span></label>' +
            '<label class="field full"><span>接口地址（OpenAI 兼容，填到 /v1 即可）</span>' +
            '<input id="set-ai-base-url" type="text" class="mono" value="' + escAttr(ai.base_url || '') + '" placeholder="https://api.deepseek.com/v1"></label>' +
            '<label class="field"><span>API Key</span><input id="set-ai-api-key" type="password" autocomplete="new-password" value="" placeholder="' + escAttr(ai.api_key ? '已设置（留空表示不修改）' : 'sk-...') + '"></label>' +
            '<label class="field"><span>模型</span><input id="set-ai-model" type="text" value="' + escAttr(ai.model || 'deepseek-chat') + '" placeholder="deepseek-chat"></label>' +
            '<label class="field full"><span>系统提示词（决定回答风格，适合朗读）</span><textarea id="set-ai-system" rows="3">' + esc(ai.system_prompt || '') + '</textarea></label>' +
            '<label class="field"><span>超时（秒）</span><input id="set-ai-timeout" type="number" min="3" max="300" value="' + escAttr(ai.timeout != null ? ai.timeout : 30) + '"></label>' +
            '<label class="field"><span>最大 tokens</span><input id="set-ai-max-tokens" type="number" min="32" max="4096" value="' + escAttr(ai.max_tokens != null ? ai.max_tokens : 300) + '"></label>' +
            '<label class="field"><span>温度 0-2</span><input id="set-ai-temperature" type="number" min="0" max="2" step="0.1" value="' + escAttr(ai.temperature != null ? ai.temperature : 0.7) + '"></label>' +
            '<button class="btn" type="submit">' + icon('i-check') + '保存 AI 设置</button>' +
            '</div>' +
            '<div class="inline-test"><input id="set-ai-test-prompt" type="text" placeholder="输入测试提示词，例如：用一句话介绍你自己" aria-label="AI 测试提示词">' +
            '<button class="btn" type="button" data-act="ai-test">' + icon('i-zap') + '生成并播报</button></div>' +
            '<p class="hint" id="set-ai-test-output"></p>' +
            (aiProbe.hint ? '<p class="hint hint-warn">' + icon('i-alert') + esc(aiProbe.hint) + '</p>' : '') +
            '</form>'
        }) +
        /* 播放器 */
        cardHTML({
          iconName: 'i-volume', title: '播放器',
          right: playerProbe && playerProbe.ready === false ? pill('未就绪', 'warn') : '',
          body: '<form data-settings-section="player" novalidate><div class="form-grid">' +
            '<label class="field"><span>播放后端</span><select id="set-player-backend">' +
            [['auto', '自动检测 bluealsa'], ['bluealsa', '强制 bluealsa'], ['mock', '模拟（不发声）']].map(function (item) {
              return '<option value="' + item[0] + '"' + ((player.backend || 'auto') === item[0] ? ' selected' : '') + '>' + item[1] + '</option>';
            }).join('') + '</select></label>' +
            '<label class="field"><span>默认音量 0-100</span><input id="set-player-volume" type="number" min="0" max="100" value="' + escAttr(player.volume != null ? player.volume : 70) + '"></label>' +
            '<label class="field full"><span>ALSA 设备模板</span><input id="set-player-alsa" type="text" class="mono" value="' + escAttr(player.alsa_device || 'bluealsa:DEV={mac},PROFILE=a2dp') + '"></label>' +
            '<label class="field"><span>最长播放秒数</span><input id="set-player-max-duration" type="number" min="5" max="86400" value="' + escAttr(player.max_duration != null ? player.max_duration : 900) + '"></label>' +
            '<label class="field"><span>循环间隙（秒）</span><input id="set-player-loop-gap" type="number" min="0" max="60" value="' + escAttr(player.loop_gap != null ? player.loop_gap : 1) + '"></label>' +
            '<label class="field full"><span>硬件音量命令（可选）</span><input id="set-player-volume-command" type="text" value="' + escAttr(player.volume_command || '') + '" placeholder="留空表示不调用"></label>' +
            '<label class="check-row full"><input id="set-player-keepalive" type="checkbox"' + (player.keep_alive ? ' checked' : '') + '><span>静音保活（空闲时保持音频流，防止蓝牙音箱自动关机）</span></label>' +
            (playerProbe && playerProbe.hint ? '<p class="hint hint-warn">' + icon('i-alert') + esc(playerProbe.hint) + '</p>' : '') +
            '<button class="btn" type="submit">' + icon('i-check') + '保存播放器设置</button>' +
            '</div></form>'
        }) +
        '</div><div class="col-5 stack">' +
        /* 蓝牙 */
        cardHTML({
          iconName: 'i-devices', title: '蓝牙',
          right: st.sim ? pill('模拟模式', 'accent') : '',
          body: '<form data-settings-section="bluetooth" novalidate><div class="form-grid">' +
            '<label class="field"><span>后端</span><select id="set-bt-backend">' +
            [['cli', 'bluetoothctl（OpenWrt）'], ['sim', '模拟设备']].map(function (item) {
              return '<option value="' + item[0] + '"' + ((bluetooth.backend || 'cli') === item[0] ? ' selected' : '') + '>' + item[1] + '</option>';
            }).join('') + '</select></label>' +
            '<label class="field"><span>bluetoothctl 命令</span><input id="set-bt-cli" type="text" value="' + escAttr(bluetooth.cli || 'bluetoothctl') + '"></label>' +
            '<label class="field"><span>适配器</span><input id="set-bt-adapter" type="text" value="' + escAttr(bluetooth.adapter || 'hci0') + '"></label>' +
            '<label class="field"><span>轮询间隔（秒）</span><input id="set-bt-poll" type="number" min="1" max="120" value="' + escAttr(bluetooth.poll_interval != null ? bluetooth.poll_interval : 5) + '"></label>' +
            '<label class="field"><span>扫描超时（秒）</span><input id="set-bt-scan-timeout" type="number" min="3" max="120" value="' + escAttr(bluetooth.scan_timeout != null ? bluetooth.scan_timeout : 12) + '"></label>' +
            '<label class="field"><span>默认设备 MAC（留空自动）</span><input id="set-bt-default-device" type="text" class="mono" value="' + escAttr(bluetooth.default_device || '') + '" placeholder="AA:BB:CC:DD:EE:FF"></label>' +
            '<label class="check-row"><input id="set-bt-auto-reconnect" type="checkbox"' + (bluetooth.auto_reconnect ? ' checked' : '') + '><span>断开后自动重连</span></label>' +
            '<label class="check-row"><input id="set-bt-trust" type="checkbox"' + (bluetooth.trust_on_connect ? ' checked' : '') + '><span>连接时自动信任设备</span></label>' +
            '<button class="btn" type="submit">' + icon('i-check') + '保存蓝牙设置</button>' +
            '</div></form>'
        }) +
        /* 安全 */
        cardHTML({
          iconName: 'i-key', title: '安全',
          right: pill(security.api_token ? '已启用令牌' : '无令牌', security.api_token ? 'ok' : 'muted'),
          body: '<form data-settings-section="security" novalidate><div class="form-grid">' +
            '<label class="field full"><span>API 访问令牌</span><input id="set-api-token" type="password" autocomplete="new-password" value="" placeholder="' + escAttr(securityHint) + '"></label>' +
            '<p class="hint">' + icon('i-info') + '设置后所有 /api 请求都需要 X-Api-Token 请求头；保存后本机会自动使用新令牌。</p>' +
            '<label class="check-row"><input id="set-allow-commands" type="checkbox"' + (security.allow_commands ? ' checked' : '') + '><span>允许规则执行系统命令（command 动作）</span></label>' +
            '<label class="field full"><span>命令前缀白名单（每行一个，留空不限制）</span>' +
            '<textarea id="set-command-allowlist" rows="3" placeholder="/root/scripts/&#10;sh /usr/share/btspeaker/">' + esc(allowlist) + '</textarea></label>' +
            '<label class="field"><span>命令超时（秒）</span><input id="set-command-timeout" type="number" min="1" max="600" value="' + escAttr(security.command_timeout != null ? security.command_timeout : 20) + '"></label>' +
            '<button class="btn" type="submit">' + icon('i-check') + '保存安全设置</button>' +
            '</div></form>'
        }) +
        /* 诊断 */
        cardHTML({
          iconName: 'i-alert', title: '运行诊断',
          right: '<span id="diag-pill-wrap">' + diagPillHTML() + '</span>' +
            '<button class="btn btn-sm btn-ghost" type="button" data-act="reload-diag">' + icon('i-refresh') + '重新检测</button>',
          body: '<div id="diag-body">' + diagBodyHTML() + '</div>'
        }) +
        '</div></div>';
    }

    render(
      pageHead('设置', 'TTS、播放器、蓝牙与安全配置',
        '<button class="btn" type="button" data-act="refresh">' + icon('i-refresh') + '重新加载</button>') +
      (sim ? '<div class="alert alert-info">' + icon('i-info') +
        '<div><strong>模拟模式已开启</strong><p>当前蓝牙、播放器与 TTS 使用模拟后端，不会操作真实硬件，适合预览与联调。配置格式与真实模式完全一致。</p></div></div>' : '') +
      formsHTML
    );
  }

  function collectSettings(section, form) {
    var current = (S.settingsConfig && S.settingsConfig[section]) || {};
    var patch = {};
    function raw(id) {
      var el = form.querySelector('#' + id);
      return el ? String(el.value == null ? '' : el.value) : '';
    }
    function trim(id) { return raw(id).trim(); }
    function num(id, fallback) {
      var n = parseInt(trim(id), 10);
      return Number.isFinite(n) ? n : fallback;
    }
    function bool(id) {
      var el = form.querySelector('#' + id);
      return !!(el && el.checked);
    }

    if (section === 'tts') {
      patch.backend = trim('set-tts-backend') || 'auto';
      patch.url = trim('set-tts-url');
      patch.method = trim('set-tts-method') || 'GET';
      patch.voice = trim('set-tts-voice') || 'zh';
      patch.speed = clampInt(num('set-tts-speed', 150), 50, 400);
      patch.format = trim('set-tts-format') || 'mp3';
      patch.espeak = trim('set-tts-espeak') || 'espeak';
      patch.timeout = clampInt(num('set-tts-timeout', 20), 3, 300);
      patch.body_template = raw('set-tts-body');
      var headersText = trim('set-tts-headers');
      if (!headersText) patch.headers = {};
      else {
        var headers = null;
        try { headers = JSON.parse(headersText); } catch (e) {
          throw new Error('请求头必须是合法 JSON：' + e.message);
        }
        if (!headers || typeof headers !== 'object' || Array.isArray(headers)) {
          throw new Error('请求头需要是 JSON 对象，例如 {"Authorization": "Bearer xxx"}');
        }
        patch.headers = headers;
      }
      return Object.assign({}, current, patch);
    }
    if (section === 'ai') {
      patch.enabled = bool('set-ai-enabled');
      patch.base_url = trim('set-ai-base-url');
      patch.model = trim('set-ai-model') || 'deepseek-chat';
      patch.system_prompt = raw('set-ai-system');
      patch.timeout = clampInt(num('set-ai-timeout', 30), 3, 300);
      patch.max_tokens = clampInt(num('set-ai-max-tokens', 300), 32, 4096);
      var aiTemp = parseFloat(trim('set-ai-temperature'));
      patch.temperature = Number.isFinite(aiTemp) ? Math.max(0, Math.min(2, aiTemp)) : 0.7;
      var aiKey = trim('set-ai-api-key');
      if (aiKey) patch.api_key = aiKey;
      return Object.assign({}, current, patch);
    }
    if (section === 'player') {
      patch.backend = trim('set-player-backend') || 'auto';
      patch.volume = clampInt(num('set-player-volume', 70), 0, 100);
      patch.alsa_device = trim('set-player-alsa');
      patch.max_duration = clampInt(num('set-player-max-duration', 900), 5, 86400);
      patch.loop_gap = clampInt(num('set-player-loop-gap', 1), 0, 60);
      patch.volume_command = trim('set-player-volume-command');
      patch.keep_alive = bool('set-player-keepalive');
      return Object.assign({}, current, patch);
    }
    if (section === 'bluetooth') {
      patch.backend = trim('set-bt-backend') || 'cli';
      patch.cli = trim('set-bt-cli') || 'bluetoothctl';
      patch.adapter = trim('set-bt-adapter') || 'hci0';
      patch.poll_interval = clampInt(num('set-bt-poll', 5), 1, 120);
      patch.scan_timeout = clampInt(num('set-bt-scan-timeout', 12), 3, 120);
      patch.default_device = trim('set-bt-default-device').toUpperCase();
      patch.auto_reconnect = bool('set-bt-auto-reconnect');
      patch.trust_on_connect = bool('set-bt-trust');
      return Object.assign({}, current, patch);
    }
    if (section === 'security') {
      patch.allow_commands = bool('set-allow-commands');
      patch.command_timeout = clampInt(num('set-command-timeout', 20), 1, 600);
      patch.command_allowlist = trim('set-command-allowlist').split('\n')
        .map(function (line) { return line.trim(); })
        .filter(function (line) { return line.length > 0; });
      var token = trim('set-api-token');
      if (token) patch.api_token = token;
      return patch;
    }
    return patch;
  }

  function saveSettingsSection(form) {
    var section = form.getAttribute('data-settings-section');
    var submit = form.querySelector('[type="submit"]');
    return busy(submit, function () {
      var patch;
      try {
        patch = collectSettings(section, form);
      } catch (err) {
        toast(err.message, 'danger');
        return;
      }
      return api('/api/settings', {
        method: 'PUT',
        body: { config: (function () { var config = {}; config[section] = patch; return config; })() },
        timeout: 20000
      }).then(function (data) {
        if (data && data.config) S.settingsConfig = data.config;
        else if (S.settingsConfig) S.settingsConfig[section] = patch;
        var restart = !!(data && data.restart_required);
        toast((SECTION_LABELS[section] || '设置') + '已保存' + (restart ? '，部分设置需重启服务后生效' : ''),
          restart ? 'warn' : 'success');
        if (section === 'security' && patch.api_token) {
          S.token = patch.api_token;
          localStorage.setItem(TOKEN_KEY, patch.api_token);
          toast('新令牌已保存在本机浏览器', 'info');
        }
        if (section === 'bluetooth' && patch.default_device !== undefined) {
          S.defaultDevice = patch.default_device;
        }
        pollStatus();
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function ttsTest(btn) {
    var input = document.getElementById('set-tts-test-text');
    var text = input ? input.value.trim() : '';
    if (!text) {
      if (input) { input.classList.add('is-invalid'); input.focus(); }
      toast('请输入测试播报文本', 'warn');
      return;
    }
    return busy(btn, function () {
      return api('/api/tts/speak', { method: 'POST', body: { text: text }, timeout: 20000 }).then(function () {
        toast('已提交测试播报，注意听音箱');
        if (input) {
          input.value = '';
          input.classList.remove('is-invalid');
        }
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function aiTest(btn) {
    var input = document.getElementById('set-ai-test-prompt');
    var prompt = input ? input.value.trim() : '';
    if (!prompt) {
      if (input) { input.classList.add('is-invalid'); input.focus(); }
      toast('请输入测试提示词', 'warn');
      return;
    }
    return busy(btn, function () {
      return api('/api/ai/chat', { method: 'POST', body: { prompt: prompt, speak: true }, timeout: 120000 }).then(function (data) {
        var text = (data && data.text) || '';
        var out = document.getElementById('set-ai-test-output');
        if (out) out.textContent = text ? 'AI 生成：' + text : '';
        if (data && data.spoken) toast('AI 已生成并播报：' + text.slice(0, 40));
        else if (data && data.tts_error) toast('AI 已生成，但播报失败：' + data.tts_error, 'warn');
        else toast('AI 已生成：' + text.slice(0, 40));
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function reloadSystemInfo(btn) {
    return busy(btn, function () {
      return loadSystem(true).then(function () {
        patchById('sysinfo-body', systemRowsHTML());
        toast('系统信息已刷新');
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  function reloadDiag(btn) {
    return busy(btn, function () {
      return loadDiag(true).then(function () {
        patchById('diag-body', diagBodyHTML());
        patchById('diag-pill-wrap', diagPillHTML());
        toast('诊断已完成');
      }).catch(function (err) {
        toast(err.message, 'danger');
      });
    });
  }

  var settingsRoute = {
    mount: function (parsed, seq) {
      renderPageSkeleton('设置', 'TTS、播放器、蓝牙与安全配置',
        '<button class="btn" type="button" data-act="refresh">' + icon('i-refresh') + '重新加载</button>');
      return ensureStatus().then(function (st) {
        if (seq !== S.navSeq) return;
        if (!st) S.routeOffline = true;
        else S.routeOffline = false;
        return Promise.all([
          loadSettingsConfig(true).catch(function () { return null; }),
          loadSystem(true).catch(function () { return null; }),
          loadDiag(true).catch(function () { return null; })
        ]).then(function (results) {
          if (seq !== S.navSeq) return;
          renderSettingsPage(results[0]);
        });
      });
    },
    sync: function () { /* 设置页不做自动重绘，避免覆盖未保存的输入 */ }
  };

  /* ---------------------------------------------------------------- 路由 */

  var routeDefs = {
    dashboard: dashboardRoute,
    devices: devicesRoute,
    rules: rulesRoute,
    settings: settingsRoute
  };

  function parseRoute() {
    var raw = String(location.hash || '').replace(/^#\/?/, '');
    var path = raw.split('?')[0];
    var parts = path.split('/').filter(function (part) { return part.length > 0; });
    var name = parts[0] || 'dashboard';
    if (ROUTES.indexOf(name) < 0) name = 'dashboard';
    return { name: name, parts: parts };
  }

  function setActiveNav(name) {
    $$('[data-nav]').forEach(function (link) {
      var active = link.getAttribute('data-nav') === name;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    $$('[data-tab]').forEach(function (link) {
      var active = link.getAttribute('data-tab') === name;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function handleRoute() {
    var parsed = parseRoute();
    S.route = parsed.name;
    S.parts = parsed.parts;
    S.navSeq += 1;
    var seq = S.navSeq;
    clearRouteTimers();
    setActiveNav(parsed.name);
    document.title = (ROUTE_TITLES[parsed.name] || '总览') + ' · 蓝音管家 BTSpeaker';
    try { window.scrollTo({ top: 0, behavior: 'auto' }); } catch (e) { window.scrollTo(0, 0); }
    var def = routeDefs[parsed.name] || dashboardRoute;
    var result;
    try {
      result = def.mount(parsed, seq);
    } catch (err) {
      if (err && err.code === 'unauthorized') return;
      console.error(err);
      renderRouteError(parsed.name, err);
      return;
    }
    Promise.resolve(result).catch(function (err) {
      if (err && err.code === 'unauthorized') return;
      console.error(err);
      if (seq === S.navSeq) renderRouteError(parsed.name, err);
    });
  }

  /* ---------------------------------------------------------------- 动作分发 */

  var ACTIONS = {
    'retry': function () {
      pollStatus();
      handleRoute();
    },
    'refresh': function (btn) {
      return busy(btn, function () { return handleRoute(); });
    },
    'open-token': function () { openTokenModal(); },
    'refresh-logs': function (btn) {
      return busy(btn, function () {
        return loadLogs(10).then(function () {
          patchById('dash-logs-body', logsBody());
          toast('日志已刷新');
        }).catch(function (err) { toast(err.message, 'danger'); });
      });
    },
    'scan-start': function (btn) { return startScan(btn); },
    'quick-play': function (btn) {
      var select = document.getElementById('quick-sound');
      if (!select) return undefined;
      return playSource(btn, select.value, selectedOptionLabel(select));
    },
    'dev-play': function (btn) {
      var select = document.getElementById('dev-sound');
      if (!select) return undefined;
      return playSource(btn, select.value, selectedOptionLabel(select));
    },
    'library-upload': function () {
      var input = document.getElementById('library-upload-input');
      if (input) input.click();
    },
    'backup-export': function (btn) { return exportBackup(btn); },
    'player-stop': function (btn) { return stopPlayer(btn); },
    'device-connect': function (btn) { return deviceAction(btn, 'connect', btn.getAttribute('data-addr'), btn.getAttribute('data-name')); },
    'device-disconnect': function (btn) { return deviceAction(btn, 'disconnect', btn.getAttribute('data-addr'), btn.getAttribute('data-name')); },
    'device-pair': function (btn) { return pairDevice(btn, btn.getAttribute('data-addr'), btn.getAttribute('data-name')); },
    'device-default': function (btn) { return setDefaultDevice(btn, btn.getAttribute('data-addr'), btn.getAttribute('data-name')); },
    'device-remove': function (btn) { return removeDevice(btn, btn.getAttribute('data-addr'), btn.getAttribute('data-name')); },
    'rules-export': function (btn) { return exportRules(btn); },
    'rules-import': function () { openImportDialog(); },
    'rule-toggle': function (btn) { toggleRule(btn); },
    'rule-run': function (btn) { return runRule(btn); },
    'rule-delete': function (btn) { return deleteRule(btn); },
    'add-condition': function () { addConditionRow(); },
    'del-condition': function (btn) {
      var row = btn.closest('[data-cond]');
      if (row && row.parentNode) row.parentNode.removeChild(row);
      refreshCondEmpty();
    },
    'add-action': function () { addActionRow(); },
    'del-action': function (btn) {
      var row = btn.closest('[data-action]');
      if (row && row.parentNode) row.parentNode.removeChild(row);
      var list = document.getElementById('action-list');
      if (list && !list.querySelector('[data-action]')) addActionRow();
      renumberActions();
    },
    'move-action-up': function (btn) { moveAction(btn, -1); },
    'move-action-down': function (btn) { moveAction(btn, 1); },
    'tts-test': function (btn) { return ttsTest(btn); },
    'ai-test': function (btn) { return aiTest(btn); },
    'reload-system': function (btn) { return reloadSystemInfo(btn); },
    'reload-diag': function (btn) { return reloadDiag(btn); }
  };

  /* ---------------------------------------------------------------- 事件绑定 */

  function bindEvents() {
    document.addEventListener('click', function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var node = target.closest('[data-act]');
      if (!node) return;
      var act = node.getAttribute('data-act');
      var handler = ACTIONS[act];
      if (!handler) return;
      event.preventDefault();
      try {
        handler(node);
      } catch (err) {
        console.error(err);
        toast('操作失败：' + (err && err.message ? err.message : '未知错误'), 'danger');
      }
    });

    document.addEventListener('submit', function (event) {
      var form = event.target;
      if (!form || form.tagName !== 'FORM') return;
      if (form.id === 'tts-quick-form') {
        event.preventDefault();
        submitQuickTts(form);
        return;
      }
      if (form.id === 'rule-form') {
        event.preventDefault();
        saveRuleForm(form);
        return;
      }
      var section = form.getAttribute('data-settings-section');
      if (section) {
        event.preventDefault();
        saveSettingsSection(form);
      }
    });

    document.addEventListener('input', function (event) {
      var target = event.target;
      if (!target) return;
      if (target.hasAttribute && target.hasAttribute('data-volume')) {
        updateVolumeLabels(target.value);
        return;
      }
      if (target.classList && target.classList.contains('is-invalid')) target.classList.remove('is-invalid');
      if (target.id === 'tts-quick-text') S.drafts.ttsText = target.value;
    });

    document.addEventListener('change', function (event) {
      var target = event.target;
      if (!target) return;
      if (target.id === 'library-upload-input') {
        var file = target.files && target.files[0];
        var uploadBtn = document.querySelector('[data-act="library-upload"]');
        if (file) uploadLibraryFile(file, uploadBtn);
        target.value = '';
        return;
      }
      if (target.hasAttribute && target.hasAttribute('data-volume')) {
        setVolumeFromInput(target);
        return;
      }
      if (target.name === 'trigger-type' || target.name === 'schedule-mode') {
        updateTriggerVisibility();
        return;
      }
      if (target.hasAttribute && target.hasAttribute('data-cond-type')) {
        var row = target.closest('[data-cond]');
        if (row) {
          var fields = row.querySelector('.cond-fields');
          if (fields) fields.innerHTML = condFieldsHTML({ type: target.value });
        }
        return;
      }
      if (target.hasAttribute && target.hasAttribute('data-action-type')) {
        var actionRow = target.closest('[data-action]');
        if (actionRow) {
          var actionFields = actionRow.querySelector('.action-fields');
          if (actionFields) actionFields.innerHTML = actionFieldsHTML({ type: target.value });
        }
        return;
      }
      if (target.id === 'quick-sound') S.drafts.quickSound = target.value;
      if (target.id === 'dev-sound') S.drafts.devSound = target.value;
    });

    window.addEventListener('hashchange', function () {
      handleRoute();
    });

    var skip = document.getElementById('skip-link');
    if (skip) {
      skip.addEventListener('click', function (event) {
        event.preventDefault();
        var view = viewEl();
        if (view) {
          view.focus();
          try { view.scrollIntoView({ block: 'start' }); } catch (e) { /* 忽略 */ }
        }
      });
    }

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        stopPolling();
      } else {
        startPolling();
      }
    });

    window.addEventListener('error', function (event) {
      if (Date.now() - lastGlobalToast < 4000) return;
      lastGlobalToast = Date.now();
      console.error(event.error || event.message);
      toast('页面出现异常：' + ((event.error && event.error.message) || event.message || '未知错误'), 'danger');
    });

    window.addEventListener('unhandledrejection', function (event) {
      if (Date.now() - lastGlobalToast < 4000) return;
      lastGlobalToast = Date.now();
      var reason = event.reason;
      if (reason && reason.code === 'unauthorized') return;
      console.error(reason);
      toast('页面出现异常：' + ((reason && reason.message) || '未知错误'), 'danger');
    });
  }

  /* ---------------------------------------------------------------- 启动 */

  function boot() {
    S.token = localStorage.getItem(TOKEN_KEY) || '';
    updateConnUI();
    updateBanner();
    bindEvents();
    if (!location.hash || location.hash.indexOf('#/') !== 0) {
      try {
        history.replaceState(null, '', '#/dashboard');
      } catch (e) {
        location.hash = '#/dashboard';
      }
    }
    startPolling();
    handleRoute();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}());
