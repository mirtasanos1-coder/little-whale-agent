/* ============================================================
 * 小鲸鱼 Agent · 首次启动依赖安装向导 (setup-wizard.js)
 * ------------------------------------------------------------
 * 解决什么问题：
 *   打包发出去之后，用户装完软件第一次打开，缺两样东西：
 *     1) NapCat（QQ 接入端，没有它收不到消息）
 *     2) edge-tts（语音引擎，没有它只能打字）
 *   老版本是在设置页里塞一行小字「点这里安装」，小白根本找不到。
 *   这里做成一个全屏向导：缺什么就列什么，能一键装的一键装，
 *   不能一键装的给官方链接 + 手动指定路径，装完才放行。
 *
 * 和后端怎么配合（都是 backend_server.py 的路由，不是 Api 方法）：
 *   GET  /api/env/check            缺什么、能不能自动装
 *   POST /api/env/install_napcat   {dir} → {job}
 *   POST /api/env/install_tts      → {job}
 *   GET  /api/env/job/{id}         轮询进度：pct / msg / lines / status
 *   POST /api/env/set_napcat_dir   {dir} 手动指定已装好的目录
 *
 * 为什么不阻塞：所有请求都是 fetch（异步），进度靠轮询 job，
 * 安装期间界面照常滚动日志，不会出现「未响应」。
 * ============================================================ */
(function () {
  'use strict';

  var WZ = {
    version: '1.2.0',
    mounted: false,
    opened: false,
    dismissed: false,       // 用户这次会话里点过「稍后再说」
    check: null,            // /api/env/check 的结果
    jobs: { napcat: null, tts: null },
    timers: { napcat: 0, tts: 0 },
    skipped: { tts: false },
    busy: {},
    lastErr: ''
  };

  // ---------------- 基础请求 ----------------
  function baseUrl() {
    try {
      if (window.__bridge && window.__bridge.resolve) return window.__bridge.resolve();
    } catch (e) { /* ignore */ }
    return Promise.resolve('http://127.0.0.1:5701');
  }

  function headers() {
    var h = { 'Content-Type': 'application/json' };
    var t = window.__API_TOKEN || (window.__BACKEND_HEALTH &&
      window.__BACKEND_HEALTH.backend && window.__BACKEND_HEALTH.backend.api_token) || '';
    if (t) h['X-API-Token'] = t;
    return h;
  }

  async function api(path, opt) {
    var b = await baseUrl();
    var o = opt || {};
    if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
    o.headers = headers();
    var r = await fetch(b + path, o);
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok && !data) return { ok: false, msg: 'HTTP ' + r.status };
    return data || { ok: false, msg: '后端没有返回内容' };
  }

  function toast(msg, type) {
    try {
      if (typeof window.toast === 'function') return window.toast(msg, type || 'info');
    } catch (e) { /* ignore */ }
    console.log('[wizard]', msg);
  }

  async function confirmBox(title, body, okText) {
    try {
      if (typeof window.confirmDialog === 'function') {
        return await window.confirmDialog(title, body || '', okText || '确定', false);
      }
    } catch (e) { /* ignore */ }
    try { return window.confirm(title + '\n\n' + (body || '')); } catch (e) { return true; }
  }

  // ---------------- 样式 ----------------
  var CSS = [
    '.sw-mask{position:fixed;inset:0;z-index:99999;display:none;align-items:center;justify-content:center;',
    'background:radial-gradient(1200px 600px at 50% -10%,rgba(14,165,233,.18),transparent),rgba(4,9,18,.86);',
    '-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);font-family:inherit;color:#e2e8f0}',
    '.sw-mask.sw-on{display:flex}',
    '.sw-box{width:min(880px,94vw);max-width:94vw;min-width:0;max-height:90vh;display:flex;flex-direction:column;border-radius:18px;',
    'background:linear-gradient(180deg,#111a2e 0%,#0c1322 100%);border:1px solid rgba(56,189,248,.22);',
    'box-shadow:0 30px 80px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.03) inset;overflow:hidden}',
    '.sw-head{display:flex;gap:14px;align-items:flex-start;padding:22px 24px 16px;border-bottom:1px solid rgba(148,163,184,.14)}',
    '.sw-logo{width:42px;height:42px;flex:none;border-radius:12px;display:flex;align-items:center;justify-content:center;',
    'background:linear-gradient(135deg,#0ea5e9,#22d3ee);font-size:22px}',
    '.sw-title{font-size:18px;font-weight:700;letter-spacing:.2px}',
    '.sw-sub{margin-top:4px;font-size:12.5px;line-height:1.6;color:#94a3b8}',
    '.sw-x{margin-left:auto;width:32px;height:32px;border-radius:9px;border:1px solid rgba(148,163,184,.2);',
    'background:transparent;color:#94a3b8;cursor:pointer;font-size:14px}',
    '.sw-x:hover{background:rgba(239,68,68,.16);color:#fca5a5;border-color:rgba(239,68,68,.4)}',
    '.sw-body{padding:16px 24px 4px;overflow:auto;display:flex;flex-direction:column;gap:14px;min-width:0}',
    '.sw-card{border:1px solid rgba(148,163,184,.16);border-radius:14px;background:rgba(15,23,42,.66);padding:16px;min-width:0}',
    '.sw-card.sw-ok{border-color:rgba(34,197,94,.35);background:rgba(16,38,30,.5)}',
    '.sw-card.sw-bad{border-color:rgba(251,191,36,.28)}',
    '.sw-ctop{display:flex;align-items:center;gap:10px;flex-wrap:wrap}',
    '.sw-cname{font-size:14.5px;font-weight:650}',
    '.sw-badge{font-size:11px;padding:2px 9px;border-radius:999px;border:1px solid transparent;white-space:nowrap}',
    '.sw-b-ok{background:rgba(34,197,94,.14);color:#4ade80;border-color:rgba(34,197,94,.35)}',
    '.sw-b-bad{background:rgba(251,191,36,.12);color:#fbbf24;border-color:rgba(251,191,36,.3)}',
    '.sw-b-run{background:rgba(56,189,248,.14);color:#38bdf8;border-color:rgba(56,189,248,.35)}',
    '.sw-b-skip{background:rgba(148,163,184,.14);color:#94a3b8;border-color:rgba(148,163,184,.3)}',
    '.sw-desc{margin-top:8px;font-size:12.5px;line-height:1.7;color:#94a3b8;',
    'overflow-wrap:anywhere;word-break:break-word}',
    '.sw-desc code{background:rgba(148,163,184,.14);padding:1px 5px;border-radius:5px;font-size:11.5px;color:#cbd5e1;',
    'word-break:break-all;overflow-wrap:anywhere}',
    '.sw-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}',
    '.sw-btn{border:1px solid rgba(148,163,184,.24);background:rgba(30,41,59,.9);color:#e2e8f0;border-radius:9px;',
    'padding:7px 14px;font-size:12.5px;cursor:pointer;transition:.15s}',
    '.sw-btn:hover:not(:disabled){background:rgba(51,65,85,.95);border-color:rgba(148,163,184,.45)}',
    '.sw-btn:disabled{opacity:.45;cursor:not-allowed}',
    '.sw-primary{background:linear-gradient(135deg,#0284c7,#0ea5e9);border-color:transparent;font-weight:600;color:#f0f9ff}',
    '.sw-primary:hover:not(:disabled){filter:brightness(1.12)}',
    '.sw-ghost{background:transparent}',
    '.sw-tip{margin-top:12px;padding:11px 13px;border-radius:11px;font-size:12px;line-height:1.75;',
    'color:#cbd5e1;background:rgba(127,29,29,.22);border:1px solid rgba(248,113,113,.35);',
    'overflow-wrap:anywhere;word-break:break-word}',
    '.sw-tip code{background:rgba(15,23,42,.7);padding:1px 5px;border-radius:5px;font-size:11.5px;word-break:break-all}',
    '.sw-row{display:flex;gap:8px;margin-top:10px}',
    '.sw-input{flex:1;min-width:0;background:rgba(2,6,23,.7);border:1px solid rgba(148,163,184,.22);border-radius:9px;',
    'padding:7px 11px;font-size:12.5px;color:#e2e8f0;outline:none}',
    '.sw-input:focus{border-color:rgba(56,189,248,.6)}',
    '.sw-prog{margin-top:12px}',
    '.sw-bar{height:7px;border-radius:99px;background:rgba(148,163,184,.16);overflow:hidden}',
    '.sw-bar>i{display:block;height:100%;width:0;border-radius:99px;',
    'background:linear-gradient(90deg,#0ea5e9,#22d3ee);transition:width .35s ease}',
    '.sw-bar.sw-fail>i{background:linear-gradient(90deg,#ef4444,#f87171)}',
    '.sw-pmsg{margin-top:6px;font-size:12px;color:#7dd3fc;word-break:break-all}',
    '.sw-log{margin-top:8px;max-height:132px;overflow:auto;background:rgba(2,6,23,.72);border:1px solid rgba(148,163,184,.14);',
    'border-radius:9px;padding:8px 10px;font-family:ui-monospace,Consolas,monospace;font-size:11.5px;',
    'line-height:1.65;color:#94a3b8;white-space:pre-wrap}',
    '.sw-log .sw-err{color:#fca5a5}',
    '.sw-foot{display:flex;align-items:center;gap:12px;padding:16px 24px;border-top:1px solid rgba(148,163,184,.14);',
    'background:rgba(2,6,23,.35)}',
    '.sw-sum{font-size:12px;color:#94a3b8;flex:1}',
    '.sw-foot .sw-btns{display:flex;gap:8px}',
    /* v1.6.2：浅色主题（樱花 / 日光白）下，向导也要跟着变白。
       原来这里全是写死的深色（遮罩 rgba(4,9,18,.86)、卡片 #0f172a），
       浅色主题用户一开程序就看见一整块黑。 */
    'html[data-theme-dark="0"] .sw-mask{color:#1f2937;background:radial-gradient(1200px 600px at 50% -10%,rgba(244,114,182,.16),transparent),rgba(255,247,250,.86)}',
    'html[data-theme-dark="0"] .sw-box{background:linear-gradient(180deg,#ffffff 0%,#fff7fa 100%);border-color:rgba(244,114,182,.28);box-shadow:0 30px 80px rgba(190,24,93,.16)}',
    'html[data-theme-dark="0"] .sw-head{border-bottom-color:rgba(148,163,184,.28)}',
    'html[data-theme-dark="0"] .sw-sub{color:#6b7280}',
    'html[data-theme-dark="0"] .sw-x{color:#6b7280;border-color:rgba(148,163,184,.38)}',
    'html[data-theme-dark="0"] .sw-card{background:#ffffff;border-color:rgba(148,163,184,.32);box-shadow:0 2px 10px rgba(15,23,42,.04)}',
    'html[data-theme-dark="0"] .sw-card.sw-ok{background:rgba(240,253,244,.9);border-color:rgba(34,197,94,.4)}',
    'html[data-theme-dark="0"] .sw-card.sw-bad{background:rgba(255,251,235,.9);border-color:rgba(251,191,36,.45)}',
    'html[data-theme-dark="0"] .sw-log{background:#f8fafc;border-color:rgba(148,163,184,.32);color:#475569}',
    'html[data-theme-dark="0"] .sw-log .sw-err{color:#dc2626}',
    'html[data-theme-dark="0"] .sw-foot{background:rgba(255,255,255,.6);border-top-color:rgba(148,163,184,.28)}',
    'html[data-theme-dark="0"] .sw-sum{color:#6b7280}',
    'html[data-theme-dark="0"] .sw-pmsg{color:#0369a1}',
    'html[data-theme-dark="0"] .sw-b-ok{color:#15803d}',
    'html[data-theme-dark="0"] .sw-b-bad{color:#b45309}'
  ].join('');

  // ---------------- DOM ----------------
  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function mount() {
    if (WZ.mounted) return;
    var st = document.createElement('style');
    st.id = 'swStyle';
    st.textContent = CSS;
    document.head.appendChild(st);

    var mask = document.createElement('div');
    mask.className = 'sw-mask';
    mask.id = 'swMask';
    mask.innerHTML = [
      '<div class="sw-box" role="dialog" aria-modal="true">',
      '  <div class="sw-head">',
      '    <div class="sw-logo">🐋</div>',
      '    <div style="flex:1">',
      '      <div class="sw-title">环境初始化向导</div>',
      '      <div class="sw-sub" id="swSub">第一次启动，先把这两样补齐；装完就能收消息、能说话了。</div>',
      '    </div>',
      '    <button class="sw-x" id="swClose" title="关闭">✕</button>',
      '  </div>',
      '  <div class="sw-body" id="swBody"></div>',
      '  <div class="sw-foot">',
      '    <div class="sw-sum" id="swSum">正在检测环境…</div>',
      '    <div class="sw-btns">',
      '      <button class="sw-btn sw-ghost" id="swRecheck">重新检测</button>',
      '      <button class="sw-btn sw-primary" id="swDone" disabled>进入控制台</button>',
      '    </div>',
      '  </div>',
      '</div>'
    ].join('');
    document.body.appendChild(mask);
    WZ.mounted = true;

    document.getElementById('swClose').addEventListener('click', function () { tryClose(); });
    document.getElementById('swRecheck').addEventListener('click', function () { recheck(); });
    document.getElementById('swDone').addEventListener('click', function () { close(true); });

    // 卡片里的按钮用事件委托，避免每次 render 重新绑定
    document.getElementById('swBody').addEventListener('click', function (ev) {
      var t = ev.target;
      if (!t || !t.dataset || !t.dataset.act) return;
      var act = t.dataset.act;
      if (act === 'napcat-auto') startNapcat();
      else if (act === 'napcat-bundle') startNapcat({ source: 'bundle' });
      else if (act === 'napcat-download') startNapcat({ source: 'download' });
      else if (act === 'napcat-import') importNapcat();
      else if (act === 'napcat-page') openDownloadPage();
      else if (act === 'napcat-browse') browseNapcat();
      else if (act === 'napcat-set') setNapcatDir(document.getElementById('swNpDir').value);
      else if (act === 'napcat-start') startNapcatApp();
      else if (act === 'napcat-webui') openWebui();
      else if (act === 'napcat-dir') openNapcatDir();
      else if (act === 'tts-auto') startTts();
      else if (act === 'tts-skip') skipTts();
    });
    document.getElementById('swBody').addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' || !ev.target) return;
      if (ev.target.id === 'swNpDir') {
        setNapcatDir(ev.target.value);
      } else if (ev.target.id === 'swNpZip') {
        importNapcat(ev.target.value);
      }
    });
  }

  // ---------------- 渲染 ----------------
  function item(key) {
    var items = (WZ.check && WZ.check.items) || [];
    for (var i = 0; i < items.length; i++) if (items[i].key === key) return items[i];
    return null;
  }

  function jobOf(key) { return WZ.jobs[key]; }

  function jobRunning(key) {
    var j = jobOf(key);
    return !!(j && (j.status === 'pending' || j.status === 'running'));
  }

  function badge(text, cls) {
    return '<span class="sw-badge ' + cls + '">' + esc(text) + '</span>';
  }

  function progressHtml(key) {
    var j = jobOf(key);
    if (!j) return '';
    var pct = Math.max(0, Math.min(100, Number(j.pct) || 0));
    var lines = (j.lines || []).slice(-8);
    var log = lines.map(function (l) {
      var isErr = /失败|错误|error|Error|无法/.test(String(l));
      return '<div class="' + (isErr ? 'sw-err' : '') + '">' + esc(l) + '</div>';
    }).join('');
    var status = j.status === 'failed' ? '（失败）' : (j.status === 'done' ? '（完成）' : '');
    return [
      '<div class="sw-prog">',
      '  <div class="sw-bar' + (j.status === 'failed' ? ' sw-fail' : '') + '"><i style="width:' + pct + '%"></i></div>',
      '  <div class="sw-pmsg">' + esc(j.msg || '') + ' · ' + pct + '%' + esc(status) + '</div>',
      log ? '<div class="sw-log" id="swLog-' + key + '">' + log + '</div>' : '',
      '</div>'
    ].join('');
  }

  function napcatCard() {
    var it = item('napcat') || {};
    var installed = !!it.installed;
    var running = !!it.running;
    var jb = jobRunning('napcat');
    var j = jobOf('napcat');
    var failed = !!(j && j.status === 'failed');
    var b = installed ? badge(running ? '已安装 · 运行中' : '已安装', 'sw-b-ok')
                      : badge('未安装', 'sw-b-bad');
    var offline = !!(it.can_install_offline);
    var desc = installed
      ? ('安装目录：<code>' + esc(it.dir || it.launcher || '（未知）') + '</code>'
        + (it.config_ok ? '　配置已生成' : '　配置未生成（点「重新检测」会补）'))
      : ((offline
          ? ('NapCat 是 QQ 接入端，没有它软件收不到任何消息。'
            + '<b style="color:#86efac">内置离线包已随程序带好</b>'
            + (it.bundled_text ? '<span style="color:#9fb0c9"> · ' + esc(it.bundled_text) + '</span>' : '')
            + '，点「一键安装（内置离线包）」几秒就装好，<b>完全不用联网</b>。'
            + '<br><span style="color:#9fb0c9">装在 <code>'
            + esc((WZ.check && WZ.check.napcat_default_dir) || 'runtime/napcat') + '</code>；'
            + '想改成在线下载最新版，点右边的「改用在线下载」。</span>')
          : ('NapCat 是 QQ 接入端，没有它软件收不到任何消息。'
            + '点「一键自动安装」会下载 Windows 免安装版并解压到 <code>'
            + esc((WZ.check && WZ.check.napcat_default_dir) || 'runtime/napcat') + '</code>。'
            + '<br><span style="color:#9fb0c9">下载自动走三个梯子：先官方 GitHub，连不上自动换国内镜像，'
            + '都不行就自己下 zip 再「手动导入安装包」—— 国内网络也能装。</span>'))
        + ((!offline && it.can_auto_install === false)
          ? '<br><span style="color:#fbbf24">⚠ 没检测到 Node.js，自动安装会失败 —— 请先装 Node，或者手动指定已装好的目录。</span>'
          : ''));

    var actions = [];
    if (!installed) {
      if (offline) {
        // v1.2.2：离线包在程序里，默认就走内置，不再让用户等网速
        actions.push('<button class="sw-btn sw-primary" data-act="napcat-bundle"' +
          (jb ? ' disabled' : '') + '>' + (jb ? '正在解压…' : '一键安装（内置离线包）') + '</button>');
        actions.push('<button class="sw-btn" data-act="napcat-download"' + (jb ? ' disabled' : '') +
          '>改用在线下载</button>');
      } else {
        actions.push('<button class="sw-btn sw-primary" data-act="napcat-auto"' +
          (jb ? ' disabled' : '') + '>' + (jb ? '正在安装…' : '一键自动安装') + '</button>');
      }
      // v1.2.1：终极兜底 —— 用户自己下好 zip 直接导入，不联网也不走代理
      actions.push('<button class="sw-btn" data-act="napcat-import"' + (jb ? ' disabled' : '') +
        '>手动导入安装包</button>');
      actions.push('<button class="sw-btn" data-act="napcat-browse"' + (jb ? ' disabled' : '') + '>浏览…</button>');
    } else {
      actions.push('<button class="sw-btn sw-primary" data-act="napcat-start"' + (running ? ' disabled' : '') + '>' +
        (running ? '已在运行' : '启动 NapCat') + '</button>');
      actions.push('<button class="sw-btn" data-act="napcat-webui">打开 WebUI 扫码</button>');
      actions.push('<button class="sw-btn" data-act="napcat-dir">打开安装目录</button>');
    }

    // 失败后的「下一步」提示条：不要只说"失败了"，得告诉人去哪下、点哪
    var failedBox = failed ? [
      '<div class="sw-tip">',
      '  <div style="color:#fca5a5;font-weight:600;margin-bottom:4px">下载没成功，别急 —— 换条路一样能装</div>',
      '  <div>① 用浏览器打开 <code>' + esc(it.official_page || 'https://github.com/NapNeko/NapCatQQ/releases') +
        '</code>（手机上也能下），选带 <b>Windows</b> 的 <code>.zip</code>；</div>',
      '  <div>② 下好后点下面的「手动导入安装包」，选中那个 zip —— 解压和写配置都交给程序；</div>',
      '  <div>③ 或者先点「打开下载页」，我在浏览器里帮你打开。</div>',
      '  <div class="sw-row" style="margin-top:6px">',
      '    <button class="sw-btn sw-primary" data-act="napcat-import">手动导入安装包</button>',
      '    <button class="sw-btn" data-act="napcat-page">打开下载页</button>',
      '  </div>',
      '</div>'
    ].join('') : '';

    var manual = installed ? '' : [
      '<div class="sw-row">',
      '  <input class="sw-input" id="swNpZip" placeholder="已经下好 zip 了？把文件路径粘这里，回车导入">',
      '  <button class="sw-btn" data-act="napcat-import"' + (jb ? ' disabled' : '') + '>导入这个文件</button>',
      '</div>',
      '<div class="sw-row">',
      '  <input class="sw-input" id="swNpDir" placeholder="已经装好了？把 NapCat 目录粘进来（含 napcat.bat）" value="',
      esc((WZ.check && WZ.check.napcat_default_dir) || ''), '">',
      '  <button class="sw-btn" data-act="napcat-set"' + (jb ? ' disabled' : '') + '>使用这个目录</button>',
      '</div>'
    ].join('');

    return [
      '<div class="sw-card ' + (installed ? 'sw-ok' : 'sw-bad') + '">',
      '  <div class="sw-ctop"><div class="sw-cname">① NapCat（QQ 接入端）</div>' + b +
        (running ? badge('WS 3001', 'sw-b-run') : '') + '</div>',
      '  <div class="sw-desc">' + desc + '</div>',
      '  <div class="sw-actions">' + actions.join('') + '</div>',
      failedBox,
      manual,
      progressHtml('napcat'),
      '</div>'
    ].join('');
  }

  function ttsCard() {
    var it = item('tts') || {};
    var installed = !!it.installed;
    var jb = jobRunning('tts');
    var skipped = WZ.skipped.tts && !installed;
    var bundled = !!it.bundled;
    var b = installed
      ? badge((bundled ? '已安装 · 内置版' : '已安装') + (it.version ? ' · ' + it.version : ''), 'sw-b-ok')
      : skipped ? badge('已跳过', 'sw-b-skip')
      : badge('未安装', 'sw-b-bad');
    var desc = installed
      ? ((bundled
          ? '语音引擎已经打进程序里了（内置版），不联网、不写系统环境、不占 C 盘，开箱即用。'
          : '语音回复可用。')
        + '想换音色去「设置 → 语音」。'
        + (it.model_dir ? ('<br><span style="color:#9fb0c9">模型/缓存目录：<code>' +
            esc(it.model_dir) + '</code>（可在「设置 → 语音」里改到别的盘）</span>') : ''))
      : (it.can_auto_install === false && it.hint
        ? esc(it.hint)
        : ('edge-tts 是语音引擎，没装只能打字回复（不影响聊天）。'
          + '装的话会调 <code>' + esc((WZ.check && WZ.check.python_for_pip) || 'python') + ' -m pip install edge-tts</code>。'));

    var actions = [];
    if (!installed) {
      if (it.can_auto_install === false) {
        // 打包版：pip 装到别的解释器里没用，别给一个点了必然失败的按钮
        actions.push('<button class="sw-btn sw-ghost" disabled>需重新打包内置</button>');
      } else {
        actions.push('<button class="sw-btn sw-primary" data-act="tts-auto"' + (jb ? ' disabled' : '') + '>' +
          (jb ? '正在安装…' : '一键安装（pip）') + '</button>');
      }
      actions.push('<button class="sw-btn sw-ghost" data-act="tts-skip"' + (jb ? ' disabled' : '') + '>' +
        (skipped ? '已跳过（点这里恢复）' : '跳过（不要语音）') + '</button>');
    }

    return [
      '<div class="sw-card ' + (installed ? 'sw-ok' : (skipped ? '' : 'sw-bad')) + '">',
      '  <div class="sw-ctop"><div class="sw-cname">② 语音引擎（edge-tts）</div>' + b + '</div>',
      '  <div class="sw-desc">' + desc + '</div>',
      '  <div class="sw-actions">' + actions.join('') + '</div>',
      progressHtml('tts'),
      '</div>'
    ].join('');
  }

  function render() {
    if (!WZ.mounted) return;
    var body = document.getElementById('swBody');
    if (!body) return;
    if (!WZ.check) {
      body.innerHTML = '<div class="sw-card"><div class="sw-desc">正在检测环境…<br>' +
        (WZ.lastErr ? '<span style="color:#fca5a5">' + esc(WZ.lastErr) + '</span>' : '') + '</div></div>';
      var sum0 = document.getElementById('swSum');
      if (sum0) sum0.textContent = WZ.lastErr ? '连不上后端' : '正在检测环境…';
      return;
    }
    body.innerHTML = napcatCard() + ttsCard();

    var nap = item('napcat') || {};
    var tts = item('tts') || {};
    var left = 0;
    if (!nap.installed) left++;
    if (!tts.installed && !WZ.skipped.tts) left++;

    var sum = document.getElementById('swSum');
    var done = document.getElementById('swDone');
    if (sum) {
      sum.innerHTML = left
        ? ('还差 <b style="color:#fbbf24">' + left + '</b> 项就能开始用了' +
           (jobRunning('napcat') || jobRunning('tts') ? '（正在安装，别关窗口）' : ''))
        : '全部就绪 ✓ 点右边进去吧';
    }
    if (done) done.disabled = left > 0;

    // 进度条滚动到底
    ['napcat', 'tts'].forEach(function (k) {
      var box = document.getElementById('swLog-' + k);
      if (box) box.scrollTop = box.scrollHeight;
    });
  }

  // ---------------- 检测 ----------------
  async function refresh() {
    WZ.lastErr = '';
    var r = null;
    try {
      r = await api('/api/env/check', { method: 'GET' });
    } catch (e) {
      WZ.lastErr = '连不上后端：' + (e && e.message ? e.message : e);
      render();
      return null;
    }
    if (!r || r.ok === false) {
      WZ.lastErr = (r && r.msg) || '环境检测失败';
      render();
      return null;
    }
    WZ.check = r;
    render();
    // v1.2.2：首次启动后端会自动展开内置 NapCat 离线包，
    // 向导打开时得「收养」这个正在跑的任务，否则用户只看到「未安装」发呆
    adoptJobs();
    return r;
  }

  // 把后端已经在跑的任务接过来显示（同一时间只收养一次）
  var _adopting = false;
  async function adoptJobs() {
    if (_adopting) return;
    _adopting = true;
    try {
      var r = await api('/api/env/jobs', { method: 'GET' });
      var items = (r && r.ok && r.items) || [];
      for (var i = 0; i < items.length; i++) {
        var j = items[i];
        var kind = j.kind === 'napcat_import' ? 'napcat' : j.kind;
        if (kind !== 'napcat' && kind !== 'tts') continue;
        if (j.status !== 'pending' && j.status !== 'running') continue;
        if (WZ.timers[kind]) continue;                       // 已经在跟了
        if (WZ.jobs[kind] && WZ.jobs[kind].status === 'done') continue;
        WZ.jobs[kind] = j;
        WZ.busy[kind] = false;
        poll(kind, j.id);
      }
    } catch (e) { /* 收养失败无所谓，用户点按钮也能重来 */ }
    _adopting = false;
  }

  async function recheck() {
    var r = await refresh();
    if (r) toast('环境检测已更新', 'ok');
  }

  // ---------------- 任务轮询 ----------------
  function stopPoll(kind) {
    if (WZ.timers[kind]) {
      clearTimeout(WZ.timers[kind]);
      WZ.timers[kind] = 0;
    }
  }

  function poll(kind, jid) {
    stopPoll(kind);
    var tick = async function () {
      var j = null;
      try {
        j = await api('/api/env/job/' + encodeURIComponent(jid), { method: 'GET' });
      } catch (e) {
        j = null;
      }
      if (j && j.ok !== false && j.id) {
        WZ.jobs[kind] = j;
        render();
        if (j.status === 'done' || j.status === 'failed') {
          WZ.timers[kind] = 0;
          var good = j.status === 'done' && (j.ok !== false);
          toast(good ? '安装完成' : ('安装失败：' + (j.msg || '')), good ? 'ok' : 'err');
          await refresh();
          onJobFinished(kind, j);
          return;
        }
      }
      WZ.timers[kind] = setTimeout(tick, 800);
    };
    WZ.timers[kind] = setTimeout(tick, 400);
  }

  function onJobFinished(kind, j) {
    if (kind !== 'napcat') return;
    var res = (j && j.result) || {};
    var it = item('napcat') || {};
    var url = res.webui || it.webui || 'http://127.0.0.1:6099/webui';
    // 装完直接把「下一步」摆到脸上：启动 → WebUI → 扫码
    confirmBox('NapCat 装好了，下一步扫码登录',
      ['1) 先点「启动 NapCat」把 QQ 登录端拉起来',
       '2) 浏览器打开 WebUI，用手机 QQ 扫码登录',
       '3) 回来点「重新检测」，看到「运行中」就成功了',
       '',
       'WebUI：' + url,
       res.webui_token ? ('临时密码（token）：' + res.webui_token) : '',
       res.config ? ('配置：' + res.config) : ''
      ].filter(function (x) { return x !== ''; }).join('\n'),
      '现在打开 WebUI').then(function (yes) {
      if (yes) openUrl(url);
    });
  }

  // ---------------- 动作 ----------------
  async function startNapcat(opts) {
    if (WZ.busy.napcat) return;
    var source = (opts && opts.source) || '';
    WZ.busy.napcat = true;
    WZ.jobs.napcat = {
      status: 'pending', pct: 1,
      msg: source === 'bundle' ? '正在展开内置离线包…' : '正在准备下载…',
      lines: []
    };
    render();
    var dir = (WZ.check && WZ.check.napcat_default_dir) || '';
    var r = null;
    try {
      r = await api('/api/env/install_napcat', { method: 'POST', body: { dir: dir, source: source } });
    } catch (e) {
      r = { ok: false, msg: String(e && e.message ? e.message : e) };
    }
    WZ.busy.napcat = false;
    if (!r || r.ok === false || !r.job) {
      WZ.jobs.napcat = { status: 'failed', pct: 100, msg: (r && r.msg) || '启动安装失败', lines: [] };
      render();
      toast('安装没能开始：' + ((r && r.msg) || '未知错误'), 'err');
      return;
    }
    if (source === 'bundle') toast('开始安装内置 NapCat —— 包就在程序里，不用联网，约 20 秒', 'info');
    else if (source === 'download') toast('开始在线下载 NapCat（约 118MB），建议先看内置离线包能不能用', 'info');
    else toast('开始安装 NapCat', 'info');
    poll('napcat', r.job);
  }

  async function startTts() {
    if (WZ.busy.tts) return;
    WZ.busy.tts = true;
    WZ.jobs.tts = { status: 'pending', pct: 3, msg: '正在准备 pip…', lines: [] };
    render();
    var r = null;
    try {
      r = await api('/api/env/install_tts', { method: 'POST', body: {} });
    } catch (e) {
      r = { ok: false, msg: String(e && e.message ? e.message : e) };
    }
    WZ.busy.tts = false;
    if (!r || r.ok === false || !r.job) {
      WZ.jobs.tts = { status: 'failed', pct: 100, msg: (r && r.msg) || '启动安装失败', lines: [] };
      render();
      toast('安装没能开始：' + ((r && r.msg) || '未知错误'), 'err');
      return;
    }
    poll('tts', r.job);
  }

  async function browseNapcat() {
    var picked = null;
    try {
      if (window.__bridge && window.__bridge.pickDir) {
        picked = await window.__bridge.pickDir('选择 NapCat 安装目录');
      } else {
        var back = await (window.pywebview.api.pick_dir('选择 NapCat 安装目录'));
        picked = { ok: !!(back && back.dir), dir: (back && back.dir) || '', msg: back && back.msg };
      }
    } catch (e) {
      picked = { ok: false, msg: String(e && e.message ? e.message : e) };
    }
    if (!picked || !picked.ok) {
      if (picked && picked.msg && picked.msg !== '已取消') toast(picked.msg, 'err');
      return;
    }
    var input = document.getElementById('swNpDir');
    if (input) input.value = picked.dir;
    setNapcatDir(picked.dir);
  }

  // v1.2.1：手动导入本地 zip（GitHub 连不上时的终极兜底，全程不联网）
  async function importNapcat(pathArg) {
    if (WZ.busy.napcat) return;
    var zp = String(pathArg || '').trim();
    if (!zp) {
      var picked = null;
      try {
        if (window.__bridge && window.__bridge.pickFile) {
          picked = await window.__bridge.pickFile({ title: '选择 NapCat 安装包（.zip）', ext: 'zip' });
        } else {
          var back = await window.pywebview.api.pick_file('选择 NapCat 安装包', 'zip');
          picked = back && back.ok ? { ok: true, path: back.path } : (back || {});
        }
      } catch (e) {
        picked = { ok: false, msg: String(e && e.message ? e.message : e) };
      }
      if (!picked || !picked.ok) {
        if (picked && picked.msg && picked.msg !== '已取消') toast(picked.msg, 'err');
        return;
      }
      zp = picked.path;
    }
    if (!/\.zip$/i.test(zp)) {
      toast('只支持 .zip 压缩包（NapCat 官方发的就是 zip）', 'err');
      return;
    }
    WZ.busy.napcat = true;
    WZ.jobs.napcat = { status: 'pending', pct: 2, msg: '正在读取压缩包…', lines: ['文件：' + zp] };
    render();
    var r = null;
    try {
      r = await api('/api/env/import_napcat', {
        method: 'POST',
        body: { zip: zp, dir: (WZ.check && WZ.check.napcat_default_dir) || '' }
      });
    } catch (e) {
      r = { ok: false, msg: String(e && e.message ? e.message : e) };
    }
    WZ.busy.napcat = false;
    if (!r || r.ok === false || !r.job) {
      WZ.jobs.napcat = { status: 'failed', pct: 100, msg: (r && r.msg) || '导入失败', lines: [] };
      render();
      toast('导入没能开始：' + ((r && r.msg) || '未知错误'), 'err');
      return;
    }
    toast('开始导入本地安装包（不联网，几秒就好）', 'info');
    poll('napcat', r.job);
  }

  function openDownloadPage() {
    var it = item('napcat') || {};
    var url = it.official_page || 'https://github.com/NapNeko/NapCatQQ/releases';
    openUrl(url);
    toast('已打开下载页：' + url, 'info');
  }

  async function setNapcatDir(dir) {
    dir = String(dir || '').trim();
    if (!dir) { toast('先选一个目录', 'err'); return; }
    var r = null;
    try {
      r = await api('/api/env/set_napcat_dir', { method: 'POST', body: { dir: dir } });
    } catch (e) {
      r = { ok: false, msg: String(e && e.message ? e.message : e) };
    }
    if (!r || r.ok === false) {
      toast((r && r.msg) || '这个目录不能用', 'err');
      return;
    }
    toast(r.msg || '已设置 NapCat 目录', 'ok');
    await refresh();
  }

  async function startNapcatApp() {
    toast('正在启动 NapCat（第一次会等它自己登录）…', 'info');
    var r = null;
    try {
      r = await window.__bridge.call('start_napcat', [25]);
    } catch (e) {
      r = { ok: false, msg: String(e && e.message ? e.message : e) };
    }
    toast((r && r.msg) || (r && r.ok ? 'NapCat 已启动' : '启动失败'), r && r.ok ? 'ok' : 'err');
    await refresh();
  }

  function skipTts() {
    WZ.skipped.tts = !WZ.skipped.tts;
    render();
    toast(WZ.skipped.tts ? '已跳过语音引擎，之后可以在设置里补装' : '好，那还是装上吧', 'info');
  }

  function openUrl(url) {
    if (!url) return;
    try { window.__bridge.call('open_url', [url]); } catch (e) { /* ignore */ }
  }

  async function openWebui() {
    var it = item('napcat') || {};
    var url = it.webui || 'http://127.0.0.1:6099/webui';
    openUrl(url);
    toast('已在浏览器打开：' + url, 'info');
  }

  async function openNapcatDir() {
    try {
      var r = await window.__bridge.call('open_napcat_dir', []);
      if (r && r.ok === false) toast(r.msg || '打不开目录', 'err');
    } catch (e) { /* ignore */ }
  }

  // ---------------- 开关 ----------------
  async function open(force) {
    mount();
    if (WZ.opened && !force) { await refresh(); return; }
    WZ.opened = true;
    WZ.dismissed = false;
    document.getElementById('swMask').classList.add('sw-on');
    document.body.style.overflow = 'hidden';
    render();
    await refresh();
  }

  // v1.3.0：公告弹窗要排在「环境初始化向导」之后（不然刚选完模式就被公告盖住）。
  // 这里给外部一个「向导结束了」的信号：正常关闭、或者根本不需要弹，都会 resolve。
  var _setupDone = null;
  window.__setupGate = new Promise(function (r) { _setupDone = r; });
  function markSetupDone() {
    try { if (_setupDone) { _setupDone(); _setupDone = null; } } catch (e) {}
  }

  function close(allDone) {
    var mask = document.getElementById('swMask');
    if (mask) mask.classList.remove('sw-on');
    document.body.style.overflow = '';
    WZ.opened = false;
    if (allDone) {
      WZ.dismissed = true;
      try { localStorage.setItem('setupWizardDone', String(Date.now())); } catch (e) {}
      toast('环境就绪，点右上角「开始」就开始收消息', 'ok');
      // v1.3.0：不在这里偷偷拉起 Agent 了 —— 装好环境 ≠ 允许它马上用你的 QQ 号说话。
      // 想「打开就自动工作」的用户可以在 设置 → 通用 打开「启动后自动工作」。
    }
    ['napcat', 'tts'].forEach(function (k) {
      if (!jobRunning(k)) stopPoll(k);
    });
    markSetupDone();
  }

  async function tryClose() {
    var nap = item('napcat') || {};
    var tts = item('tts') || {};
    var left = [];
    if (!nap.installed) left.push('NapCat（没它收不到 QQ 消息）');
    if (!tts.installed && !WZ.skipped.tts) left.push('语音引擎（不装就只能打字）');
    if (!left.length) { close(true); return; }
    var yes = await confirmBox('还没装完，确定要跳过？',
      '还差：\n' + left.join('\n') + '\n\n跳过之后，可以在「设置 → 环境」里重新打开这个向导。',
      '仍然跳过');
    if (!yes) return;
    WZ.dismissed = true;
    try { localStorage.setItem('setupWizardSkipped', String(Date.now())); } catch (e) {}
    close(false);
  }

  // ---------------- 自动打开 ----------------
  async function autoCheck() {
    var r = await refresh();
    if (!r) { markSetupDone(); return; }
    if (!r.needs_setup) {
      // 都装好了什么都不做（不打扰老用户）
      try { localStorage.setItem('setupWizardDone', String(Date.now())); } catch (e) {}
      markSetupDone();
      return;
    }
    var skipped = 0;
    try { skipped = parseInt(localStorage.getItem('setupWizardSkipped') || '0', 10) || 0; } catch (e) {}
    // 跳过过的，24 小时内不再自动弹（免得每次开都烦人），但可以手动 open()
    if (skipped && Date.now() - skipped < 24 * 3600 * 1000) { markSetupDone(); return; }
    mount();
    await open();
  }

  // ---------------- 对外接口 ----------------
  window.SetupWizard = {
    version: WZ.version,
    open: function () { return open(true); },
    close: function () { return close(false); },
    state: function () { return WZ; },
    check: function () { return refresh(); },
    auto: autoCheck
  };

  function boot() {
    // v1.3.0：不要在免责协议/性能模式之前抢着弹出来（老版本就是这里抢的）。
    // first-run.js 会把 window.__firstRunGate 挂上：协议没同意、模式没选，这儿就等着。
    var gate = window.__firstRunGate || Promise.resolve();
    gate.then(function () {
      // 等桥接层把后端端口和令牌拿到再检测，否则第一下必然连不上
      var t = setTimeout(function () { autoCheck(); }, 600);
      window.addEventListener('pywebviewready', function () {
        clearTimeout(t);
        setTimeout(function () { autoCheck(); }, 400);
      }, { once: true });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
