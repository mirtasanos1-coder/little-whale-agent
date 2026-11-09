/* ============================================================
 * 小鲸鱼 Agent · Tauri 前端桥接层 (bridge.js)
 * ------------------------------------------------------------
 * 作用：把原来的 pywebview 桥（window.pywebview.api.xxx(...)）
 *       换成对 Python 后端（backend_server.py，FastAPI）的
 *       fetch('http://127.0.0.1:端口/api/xxx') 调用。
 *
 * 为什么做成「代理对象」而不是改几百处调用：
 *   index.html 里有 200 多处 call('方法名', …) / API['方法名'](…)，
 *   全部重写风险大。这里给 window.pywebview.api 挂一个 Proxy，
 *   任何方法名都会变成一次 HTTP 请求 —— 前端一行都不用改。
 *
 * 日志：不再轮询 get_logs，而是连 ws://…/ws/logs 增量收，
 *       在本地维护一份文本缓冲；前端仍然调 get_logs(offset)，
 *       这里用缓冲里的增量回答它（对前端完全透明）。
 * ============================================================ */
(function () {
  'use strict';

  var VERSION = '1.2.0';

  // 后端可能在 5700 起往后找空端口（5700 留给 OneBot webhook，
  // 5709 留给 Agent 的单实例锁 —— 这两个都不是 HTTP 服务，别去戳）
  var CANDIDATE_PORTS = [5701, 5702, 5703, 5704, 5705, 5706, 5707, 5708, 5710,
                         5711, 5712, 5713, 5714, 5715, 5716, 5717, 5700];

  // 不可逆操作：必须先换一次性票据（POST /api/confirm/ticket，30 秒、用完即焚）。
  // 口令不再从 /api/health 下发（QA BUG-04）。
  var DANGER = ['run_uninstall', 'apply_update', 'rollback_update',
                'memory_rebuild_run', 'do_memory_import', 'do_memory_import_dir',
                'memory_import', 'import_memory', 'import_diary_backup'];

  var base = '';
  var basePromise = null;
  // v1.9.0：写操作要带 Token。它由壳生成、经 backend_info 下发，只活在这个页面里，
  // 不落盘、不进命令行 —— 本机别的进程读不到，就没法替用户改配置。
  var apiToken = '';

  function setToken(t) {
    var v = t ? String(t) : '';
    if (v) apiToken = v;
    return apiToken;
  }

  function tokenHeader(h) {
    if (apiToken) h['X-API-Token'] = apiToken;
    return h;
  }
  var ws = null;
  var wsState = 'idle';          // idle | connecting | open | closed
  var readyFired = false;

  // ---------- 日志缓冲（WS 喂，get_logs 读） ----------
  var LOG = { text: '', epoch: 0, truncated: false };
  var MAX_LOG_CHARS = 4 * 1024 * 1024;   // 缓冲上限 4MB，超了从头裁掉

  function logAppend(line) {
    if (line === undefined || line === null) return;
    LOG.text += String(line) + '\n';
    if (LOG.text.length > MAX_LOG_CHARS) {
      LOG.text = LOG.text.slice(LOG.text.length - MAX_LOG_CHARS * 0.8);
      LOG.truncated = true;
    }
  }

  function logReset() {
    LOG.text = '';
    LOG.epoch += 1;
    LOG.truncated = false;
  }

  // ---------- 基础工具 ----------
  function fetchTimeout(url, opt, ms) {
    var ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = null;
    var o = opt || {};
    if (ctl) {
      o.signal = ctl.signal;
      timer = setTimeout(function () { try { ctl.abort(); } catch (e) {} }, ms || 1500);
    }
    return fetch(url, o).then(function (r) {
      if (timer) clearTimeout(timer);
      return r;
    }, function (e) {
      if (timer) clearTimeout(timer);
      throw e;
    });
  }

  // ---------- 找后端端口 ----------
  // v1.8.10：拿到地址先「真敲一下」再认。
  // 以前直接采信 backend_info 里的 state.base —— 那是后端启动时记下的端口，
  // 后端崩掉重生/换了端口之后它还是旧的，前端就会永远指着一个死端口，
  // 表现就是「挂一会儿就掉后端」「保存并重启 Agent 之后连不上后端」。
  function pingBase(b, ms) {
    return fetchTimeout(b + '/api/ping', { method: 'GET' }, ms || 800)
      .then(function (r) { return !!(r && r.ok); })
      .catch(function () { return false; });
  }

  function invalidateBase() {
    base = '';
    basePromise = null;
  }

  async function resolveBase() {
    if (base) return base;
    if (basePromise) return basePromise;

    basePromise = (async function () {
      // 1) 打包后由 Rust 端告诉前端真实端口（最可靠）—— 但要先探活
      var injected = (window.__API_BASE || '').replace(/\/+$/, '');
      if (injected && await pingBase(injected, 900)) { base = injected; return injected; }

      if (window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke) {
        try {
          var info = await window.__TAURI__.core.invoke('backend_info');
          if (info && info.token) setToken(info.token);
          var ib = info && info.base ? String(info.base).replace(/\/+$/, '') : '';
          if (ib && await pingBase(ib, 900)) { base = ib; return ib; }
        } catch (e) { /* Rust 端还没起来，继续扫端口 */ }
      }

      // 2) 扫端口（这一步才是「后端换端口了也能找回来」的保证）
      for (var i = 0; i < CANDIDATE_PORTS.length; i++) {
        var u = 'http://127.0.0.1:' + CANDIDATE_PORTS[i];
        if (await pingBase(u, 700)) { base = u; return u; }
      }

      // 3) 都没找到：这次先按默认值试，但**不缓存**，下次重新找
      return 'http://127.0.0.1:5701';
    })();

    try {
      return await basePromise;
    } finally {
      basePromise = null;
    }
  }

  async function loadHealth() {
    var b = await resolveBase();
    try {
      var r = await fetchTimeout(b + '/api/health', { method: 'GET' }, 3000);
      if (r && r.ok) {
        var h = await r.json();
        if (h && h.backend) {
          window.__BACKEND_HEALTH = h;
        }
        return h;
      }
    } catch (e) { /* 后端没起来也不该卡住界面 */ }
    return null;
  }

  // ---------- 一次性确认票据（V1.0.0-Beta 安全整改 / QA BUG-04）----------
  // 以前：后端把长期口令放在 /api/health 里，前端读一次存在内存里，危险操作带 X-Confirm-Action。
  //       问题：任意本机网页只要读到 health 就能拿着口令卸载程序。
  // 现在：真要动危险操作时，先 POST /api/confirm/ticket 换一张 30 秒、一次性、
  //       绑定方法名的票据，再带 X-Confirm-Ticket。health 里已经没有口令了。
  async function fetchTicket(method) {
    var b = await resolveBase();
    try {
      var r = await fetchTimeout(b + '/api/confirm/ticket', {
        method: 'POST',
        // v1.9.1：票据接口本身也要 Token —— 抢救四把它从免鉴权白名单里移出去了。
        // 这里忘了带的话，所有危险操作（卸载/更新/回滚/记忆重建/导入）都会先吃
        // 一个 401，拿到空票据，然后被 403 挡住。
        headers: tokenHeader({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ method: method })
      }, 5000);
      if (!r || !r.ok) return '';
      var d = await r.json();
      return (d && d.ok && d.nonce) ? String(d.nonce) : '';
    } catch (e) {
      return '';
    }
  }

  // ---------- 一次 RPC ----------
  async function rpcOnce(method, args) {
    var b = await resolveBase();
    var headers = tokenHeader({ 'Content-Type': 'application/json' });
    var ticket = '';
    if (DANGER.indexOf(method) >= 0) {
      ticket = await fetchTicket(method);
      if (ticket) headers['X-Confirm-Ticket'] = ticket;
    }

    var r;
    try {
      r = await fetchTimeout(b + '/api/' + encodeURIComponent(method), {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({ args: args || [] })
      }, 60000);
    } catch (e) {
      // 后端崩了/端口变了：清掉缓存，下次重新探测
      if (e && e.name === 'AbortError') return { __net: 'timeout', ok: false, msg: method + ' 超时（后端 60 秒没回）' };
      invalidateBase();
      return { __net: 'down', ok: false, msg: '连不上后端（' + (e && e.message ? e.message : e) + '）' };
    }

    if (r.status === 404) return { ok: false, msg: '后端没有这个方法：' + method };
    if (r.status === 403) {
      // 免责协议没同意 / 来源被拒：把后端那句人话原样交给界面
      try { return await r.json(); } catch (e0) { return { ok: false, msg: '被后端拒绝（403）：' + method }; }
    }
    if (r.status === 415) {
      try { return await r.json(); } catch (e0) { return { ok: false, msg: '请求格式被拒（415）：' + method }; }
    }
    if (r.status === 412) {
      var t = null;
      try { t = await r.json(); } catch (e2) {}
      return t || { ok: false, msg: '后端要求确认：' + method };
    }
    if (!r.ok) return { ok: false, msg: 'HTTP ' + r.status + '：' + method };

    var data = null;
    try { data = await r.json(); } catch (e3) { return { ok: false, msg: '返回不是 JSON：' + method }; }

    // 危险操作被拦（票据没拿到/过期）：现场再换一张票重试一次
    if (data && data.ok === false && data.danger) {
      var t2 = await fetchTicket(method);
      if (t2) {
        try {
          var r2 = await fetchTimeout(b + '/api/' + encodeURIComponent(method), {
            method: 'POST',
            headers: tokenHeader({ 'Content-Type': 'application/json', 'X-Confirm-Ticket': t2 }),
            body: JSON.stringify({ args: args || [] })
          }, 60000);
          if (r2 && r2.ok) data = await r2.json();
        } catch (e4) { /* 保持原来的拒绝结果 */ }
      }
    }
    return data;
  }

  // v1.8.10：网络层失败（连不上/连接被掐）自动换地址重试，最多 3 次。
  // 后端重启、Agent 重启、后端崩了重生这些瞬间，用户不该看到一个红色「连不上后端」。
  async function rpc(method, args) {
    for (var attempt = 0; ; attempt++) {
      var res = await rpcOnce(method, args);
      if (!res || !res.__net) return res;
      var kind = res.__net;
      try { delete res.__net; } catch (e) { }
      invalidateBase();
      if (kind === 'timeout') return res;
      if (attempt >= 2) return res;
      await new Promise(function (r) { setTimeout(r, 300 + attempt * 700); });
    }
  }

  // ---------- 日志 WS ----------
  function openLogWs() {
    resolveBase().then(function (b) {
      if (ws || !b) return;
      var url = b.replace(/^http/, 'ws') + '/ws/logs?from_start=1';
      wsState = 'connecting';
      try {
        ws = new WebSocket(url);
      } catch (e) {
        wsState = 'closed';
        return;
      }

      ws.onopen = function () { wsState = 'open'; };

      ws.onmessage = function (ev) {
        var msg = null;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (!msg) return;
        if (msg.type === 'hello') {
          // 后端从头开始推：清空缓冲，让前端下一次 get_logs(0) 拿到全量
          logReset();
          return;
        }
        if (msg.type === 'log' && msg.lines && msg.lines.length) {
          for (var i = 0; i < msg.lines.length; i++) logAppend(msg.lines[i]);
          return;
        }
      };

      ws.onclose = function () {
        wsState = 'closed';
        ws = null;
        // 后端被重启（比如用户改了模型配置）后要能自己连回来
        setTimeout(function () { if (!ws) openLogWs(); }, 2500);
      };

      ws.onerror = function () { /* onclose 会跟上 */ };
    });
  }

  // ---------- 暴露给 index.html 的「api 对象」 ----------
  function localGetLogs(offset) {
    var off = Number(offset) || 0;
    if (off > LOG.text.length) {
      // 前端记的偏移比我们缓冲大（比如缓冲被截断过）→ 让它重置
      return { ok: true, reset: true, text: LOG.text, offset: LOG.text.length };
    }
    return {
      ok: true,
      reset: false,
      text: LOG.text.slice(off),
      offset: LOG.text.length,
      epoch: LOG.epoch
    };
  }

  // ---------- 窗口控制：交给 Tauri，不再走 Python ----------
  // v1.2.0 重新适配无边框：窗口是无边框的（decorations:false），标题栏由
  // index.html 自己画（#titlebar + .fb-grip 八个热区）。原来这些按钮
  // 调的是 pywebview 的 win_min / win_toggle_max / win_close，现在后端
  // 那三个方法只是空壳（返回 headless:true），真正干活的是这里。
  function tauriWin() {
    try {
      var T = window.__TAURI__;
      if (!T) return null;
      if (T.window && T.window.getCurrentWindow) return T.window.getCurrentWindow();
      if (T.webviewWindow && T.webviewWindow.getCurrentWindow) {
        return T.webviewWindow.getCurrentWindow();
      }
    } catch (e) { /* 浏览器调试环境没有 __TAURI__ */ }
    return null;
  }

  var WIN_DIRS = {
    n: 'North', s: 'South', w: 'West', e: 'East',
    nw: 'NorthWest', ne: 'NorthEast', sw: 'SouthWest', se: 'SouthEast'
  };

  // ---------- v1.3.0：无边框 / 托盘 真实开关 ----------
  // v1.8.2：初值给 true（无边框）。给 false 的话，只要启动时读不到状态，
  // 就会退回有边框，把系统标题栏重新顶出来。
  var UI = { frameless: true, tray: false };

  function applyFrameless(flag) {
    var T = window.__TAURI__;
    UI.frameless = !!flag;
    try { document.body.classList.toggle('frameless', UI.frameless); } catch (e) {}
    if (T && T.core && T.core.invoke) {
      return T.core.invoke('set_decorations', { flag: !UI.frameless })
        .then(function (r) { return { ok: !!(r && r.ok), frameless: UI.frameless, decorations: !UI.frameless }; })
        .catch(function (e) { return { ok: false, msg: '切换边框失败：' + (e && e.message ? e.message : e) }; });
    }
    return Promise.resolve({ ok: true, frameless: UI.frameless, headless: true });
  }

  function setTray(enabled) {
    var T = window.__TAURI__;
    if (!(T && T.core && T.core.invoke)) return Promise.resolve({ ok: true, headless: true });
    return T.core.invoke('set_tray', { enabled: !!enabled })
      .then(function (r) { if (r && r.ok) UI.tray = !!r.tray; return r; })
      .catch(function (e) { return { ok: false, msg: '托盘开关失败：' + (e && e.message ? e.message : e) }; });
  }

  function windowMethod(name, args) {
    var w = tauriWin();
    if (!w) return Promise.resolve({ ok: false, headless: true, msg: '当前不是 Tauri 窗口' });
    try {
      if (name === 'win_min') {
        w.minimize();
        return Promise.resolve({ ok: true, headless: false });
      }
      if (name === 'win_toggle_max') {
        return w.isMaximized().then(function (mx) {
          if (mx) { w.unmaximize(); } else { w.maximize(); }
          return { ok: true, headless: false, maximized: !mx };
        });
      }
      if (name === 'win_close') {
        w.close();
        return Promise.resolve({ ok: true, headless: false, msg: '正在关闭…' });
      }
      if (name === 'win_state') {
        // v1.3.0：frameless 以「窗口真实有没有系统边框」为准，
        // 这样设置里的开关才能关得掉（之前这里写死 true，开关点了没用）
        var T = window.__TAURI__;
        var realDeco = T && T.core
          ? T.core.invoke('get_decorations').catch(function () { return null; })
          : Promise.resolve(null);
        return Promise.all([w.isMaximized(), realDeco]).then(function (pair) {
          var mx = pair[0], d = pair[1];
          var hasDeco = (d && typeof d.decorations === 'boolean') ? d.decorations : !UI.frameless;
          return {
            ok: true, headless: false, frameless: !hasDeco, decorations: !!hasDeco,
            tray_running: !!UI.tray, maximized: !!mx,
            width: window.outerWidth || window.innerWidth,
            height: window.outerHeight || window.innerHeight
          };
        });
      }
      if (name === 'win_resize') {
        var wd = Number(args && args[0]) || 0;
        var ht = Number(args && args[1]) || 0;
        if (wd > 300 && ht > 200 && w.setSize) {
          var LS = window.__TAURI__.dpi && window.__TAURI__.dpi.LogicalSize;
          w.setSize(LS ? new LS(wd, ht) : { width: wd, height: ht });
        }
        return Promise.resolve({ ok: true, headless: false });
      }
      if (name === 'tray_start') {
        return setTray(true);
      }
      if (name === 'tray_stop') {
        return setTray(false);
      }
      if (name === 'set_frameless') {
        return applyFrameless(!!(args && args[0]));
      }
      if (name === 'restart_app') {
        var T2 = window.__TAURI__;
        if (T2 && T2.core && T2.core.invoke) {
          return T2.core.invoke('app_relaunch')
            .then(function (r) { return r || { ok: true }; })
            .catch(function (e) { return { ok: false, msg: String(e) }; });
        }
        return Promise.resolve({ ok: true, msg: '源码模式请手动重启' });
      }
    } catch (e) {
      return Promise.resolve({ ok: false, msg: '窗口操作失败：' + (e && e.message ? e.message : e) });
    }
    return null;
  }

  // 文件/目录选择：优先用 Tauri 的对话框（模态、跟窗口一体），
  // 没有 Tauri 就退回后端的 tkinter 对话框（浏览器调试时用）。
  async function pickDir(title) {
    var T = window.__TAURI__;
    if (T && T.core && T.core.invoke) {
      try {
        var r = await T.core.invoke('plugin:dialog|open', {
          options: { directory: true, multiple: false, title: title || '选择目录' }
        });
        var p = (r && typeof r === 'object' && !Array.isArray(r)) ? (r.path || r.value || '') : r;
        if (Array.isArray(p)) p = p[0] || '';
        if (p) return { ok: true, dir: String(p) };
        if (r === null || p === '') return { ok: false, msg: '已取消', dir: '' };
      } catch (e) { /* 插件没装/没权限 → 走兜底 */ }
    }
    var back = await rpc('pick_dir', [title || '选择目录']);
    if (back && back.dir) return { ok: true, dir: back.dir };
    return { ok: false, msg: (back && back.msg) || '已取消', dir: '' };
  }

  // v1.2.1：选文件（向导里「手动导入安装包」用）。opts: {title, ext}
  async function pickFile(opts) {
    opts = opts || {};
    var title = opts.title || '选择文件';
    var ext = String(opts.ext || 'zip').replace(/^\./, '');
    var T = window.__TAURI__;
    if (T && T.core && T.core.invoke) {
      try {
        var r = await T.core.invoke('plugin:dialog|open', {
          options: {
            directory: false,
            multiple: false,
            title: title,
            filters: [{ name: ext.toUpperCase() + ' 文件', extensions: [ext] },
                      { name: '所有文件', extensions: ['*'] }]
          }
        });
        var p = (r && typeof r === 'object' && !Array.isArray(r)) ? (r.path || r.value || '') : r;
        if (Array.isArray(p)) p = p[0] || '';
        if (p) return { ok: true, path: String(p) };
        if (r === null || p === '') return { ok: false, msg: '已取消', path: '' };
      } catch (e) { /* 插件没装/没权限 → 走兜底 */ }
    }
    var back = await rpc('pick_file', [title, ext]);
    if (back && back.ok && back.path) return { ok: true, path: back.path };
    return { ok: false, msg: (back && back.msg) || '已取消', path: '' };
  }

  // ---------- 无边框：把 pywebview 那套热区接到 Tauri 上 ----------
  // 1) 标题栏改为 data-tauri-drag-region（拖拽/双击最大化由 Tauri 原生处理）
  // 2) 八个 .fb-grip 热区改成调用原生 startResizeDragging（比原来的
  //    「算位移 + win_resize」平滑得多，也支持拖动窗口边缘）
  // 3) 最大化状态变化时同步界面上那个 □ / ❐ 图标
  function adoptFrameless() {
    var tb = document.getElementById('titlebar');
    var drag = document.getElementById('tbDrag');
    [tb, drag].forEach(function (el) {
      if (!el) return;
      el.setAttribute('data-tauri-drag-region', '');
    });
    if (drag) {
      // 标题文字/图标本身也要能拖动（否则鼠标压在文字上就拖不动）
      Array.prototype.forEach.call(drag.children || [], function (c) {
        try { c.setAttribute('data-tauri-drag-region', ''); } catch (e) {}
      });
    }
    var w = tauriWin();
    if (w && !adoptFrameless.wired) {
      adoptFrameless.wired = true;
      try {
        w.onResized(function () { syncMaxBtn(); });
        syncMaxBtn();
      } catch (e) { /* 老版本没有 onResized，忽略 */ }
    }
    // 捕获阶段拦下 .fb-grip 的 pointerdown：让它走原生缩放，不再跑
    // index.html 里那套「鼠标位移 → win_resize」的老逻辑
    if (!adoptFrameless.gripsWired) {
      adoptFrameless.gripsWired = true;
      document.addEventListener('pointerdown', function (ev) {
        var t = ev.target;
        var cls = (t && t.className) ? String(t.className) : '';
        if (cls.indexOf('fb-grip') < 0) return;
        var dir = '';
        Object.keys(WIN_DIRS).forEach(function (k) {
          if (cls.indexOf(' ' + k) >= 0) dir = k;
        });
        var tw = tauriWin();
        if (!dir || !tw || !tw.startResizeDragging) return;
        // 只处理无边框模式（.frameless 时才显示热区）
        if (document.body.className.indexOf('frameless') < 0) return;
        ev.preventDefault();
        ev.stopPropagation();
        try { tw.startResizeDragging(WIN_DIRS[dir]); } catch (e) {}
      }, true);
    }
  }

  function syncMaxBtn() {
    var w = tauriWin();
    var btn = document.getElementById('tbMax');
    if (!w || !btn) return;
    try {
      w.isMaximized().then(function (mx) {
        // v1.8.2：图标改成 SVG，用 data-state 切，不能写 textContent
        btn.setAttribute('data-state', mx ? 'restore' : 'max');
        btn.title = mx ? '还原' : '最大化 / 还原';
      });
    } catch (e) {}
  }

  var methodCache = Object.create(null);

  function makeMethod(name) {
    if (methodCache[name]) return methodCache[name];
    var fn = function () {
      var args = Array.prototype.slice.call(arguments);
      var local = windowMethod(name, args);
      if (local) return local;
      if (name === 'get_logs') {
        // 用 WS 缓冲回答，避免每秒一次 HTTP 长轮询
        var es = resolveBase();
        return es.then(function () { return localGetLogs(args[0]); });
      }
      if (name === 'clear_logs') {
        return rpc(name, args).then(function (r) {
          if (r && r.ok !== false) logReset();
          return r;
        });
      }
      if (name === 'get_status') {
        // 后端 get_status 的 frameless 读的是配置文件，而"实际有没有系统边框"
        // 由 UI.frameless 说了算（设置里能真切）。这里按真实值改写，
        // renderStatus 每 4 秒刷一次也不会把自绘标题栏抹掉。
        return rpc(name, args).then(function (r) {
          if (r && typeof r === 'object') r.frameless = UI.frameless;
          return r;
        });
      }
      if (name === 'quit_app') {
        // v1.6.0：后端退出不代表壳退出。以前点「退出」只把后端收了，
        // 窗口还在（开了托盘更是永远留在托盘里），用户以为点了没反应。
        return rpc(name, args).then(function (r) {
          var T = window.__TAURI__;
          if (T && T.core && T.core.invoke) {
            setTimeout(function () {
              try { T.core.invoke('app_quit'); } catch (e) {}
            }, 400);
          } else if (window.close) {
            setTimeout(function () { try { window.close(); } catch (e) {} }, 400);
          }
          return r;
        });
      }
      return rpc(name, args);
    };
    methodCache[name] = fn;
    return fn;
  }

  var apiProxy = new Proxy({}, {
    get: function (_t, prop) {
      if (prop === 'then' || prop === 'toJSON' || typeof prop !== 'string') return undefined;
      return makeMethod(prop);
    },
    has: function () { return true; },
    set: function () { return true; }
  });

  // ---------- pywebview 兼容层 ----------
  window.pywebview = { api: apiProxy };

  // 让「文档里同时存在 Tauri / 浏览器」两种打开方式都能工作
  window.__bridge = {
    version: VERSION,
    get base() { return base; },
    resolve: resolveBase,
    health: loadHealth,
    call: function (m, args) { return rpc(m, Array.isArray(args) ? args : [args]); },
    logText: function () { return LOG.text; },
    wsState: function () { return wsState; },
    pickDir: pickDir,
    pickFile: pickFile,
    tauriWin: tauriWin,
    windowMethod: windowMethod
  };

  // ---------- 通知 index.html「接口就绪」（原来由 pywebview 触发） ----------
  // v1.8.10：心跳。挂在前台久了后端可能崩掉/换端口，只靠「用户点一下」才发现太晚。
  // 每 20 秒静默探一次，连续 2 次不通就清掉缓存地址，下次调用自动重找。
  var _hbFail = 0;
  setInterval(function () {
    if (!base) { resolveBase(); return; }
    pingBase(base, 2500).then(function (ok) {
      if (ok) { _hbFail = 0; return; }
      _hbFail += 1;
      if (_hbFail >= 2) {
        _hbFail = 0;
        invalidateBase();
        resolveBase();
      }
    });
  }, 20000);

  async function startup() {
    await loadHealth();      // 顺便拿到确认令牌 + 后端状态
    openLogWs();             // 日志走 WS
    adoptFrameless();        // 无边框标题栏/热区接到 Tauri
    // v1.3.0：按配置把「真实窗口边框 / 托盘」一次性对齐
    // 注意：后端可能还在启动，拿不到明确布尔值时**什么都别做** ——
    // 否则会把 frameless=false 当成真值，反而把系统标题栏强加上去。
    for (var attempt = 0; attempt < 8; attempt++) {
      var st0 = null;
      try { st0 = await rpc('get_status', []); } catch (e) { st0 = null; }
      if (st0 && st0.ok !== false && typeof st0.frameless === "boolean") {
        // v1.8.2：先看窗口「真实」有没有系统边框，只有和期望不一致才动手。
        // 以前是读到 frameless=false 就直接 applyFrameless(false)，
        // 那会把系统标题栏重新加回来，跟 tauri.conf.json 的 decorations:false 打架。
        var wantFl = st0.frameless === true;
        var realDeco = null;
        try {
          var T0 = window.__TAURI__;
          if (T0 && T0.core && T0.core.invoke) {
            var dd = await T0.core.invoke("get_decorations").catch(function () { return null; });
            if (dd && typeof dd.decorations === "boolean") realDeco = dd.decorations;
          }
        } catch (e0) { realDeco = null; }
        if (wantFl) {
          // 要无边框：只有真的还带着系统边框时才去摘掉
          if (realDeco === true) {
            await applyFrameless(true);
          } else {
            UI.frameless = true;
            try { document.body.classList.add("frameless"); } catch (e1) {}
          }
        } else if (realDeco !== true) {
          // 配置明确要系统边框（用户自己关掉了无边框）才加回来
          await applyFrameless(false);
        }
        if (typeof st0.tray_enabled === "boolean") await setTray(st0.tray_enabled);
        break;
      }
      await new Promise(function (r) { setTimeout(r, 1000); });
    }
    window.dispatchEvent(new Event('pywebviewready'));
    // 少数环境里事件派发得比监听注册早，补一次兜底（index.html 里 boot 有幂等判断）
    if (!readyFired) {
      readyFired = true;
      setTimeout(function () { window.dispatchEvent(new Event('pywebviewready')); }, 300);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(startup, 0); });
  } else {
    setTimeout(startup, 0);
  }

  // 后端换了端口/重启后，让 Rust 端能主动刷新前端连接
  window.addEventListener('backend-port-changed', function (ev) {
    var nb = (ev && ev.detail) || '';
    if (!nb) return;
    base = String(nb).replace(/\/+$/, '');
    basePromise = null;
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    openLogWs();
  });
})();
