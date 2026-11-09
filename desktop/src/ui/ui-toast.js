/* ============================================================================
   小鲸鱼 · 轻量提示 Toast（V1.7.0）
   ----------------------------------------------------------------------------
   UI.toast(msg, type, options)
     msg      : 字符串（多行用 \n）
     type     : success | error | info | warning
                （兼容旧写法 ok / err / warn，内部自动映射）
     options  :
       duration   : 毫秒，默认 2600（需求要求 2-3 秒）；传 0 表示不自动消失
       onRetry    : 函数 → 出现【重试】按钮，点击后立即执行并关闭提示
       onDetails  : 函数 → 出现【查看详情】按钮
       undoCallback: 函数 → 出现【撤销】按钮（点击执行后关闭）
       actions    : [{label, onClick, kind}] 自定义按钮，kind: 'primary'|'danger'|''
       key        : 自定义去重键（默认 type + 文本）
       sticky     : true = 不自动消失（等价 duration:0）
       id         : 复用已有提示的 id（同 id 会原地更新而不是新建）

   行为约定（需求原文）：
     · 不阻断：不锁滚动、不抢焦点、不盖遮罩
     · 同内容不叠加：重复触发只刷新倒计时并显示 ×N
     · 最多同时 3 条，超出的把最老的一条挤掉
     · 自动淡出
   ========================================================================== */
(function (global) {
  'use strict';

  var UI = global.UI;
  if (!UI || !UI.__core_ready) {
    // 底座没加载就退化成原生提示，保证不会静默丢消息
    global.UI = global.UI || {};
    global.UI.toast = function (msg) { try { console.log('[toast]', msg); } catch (e) {} };
    return;
  }
  if (UI.__toast_ready) return;
  UI.__toast_ready = true;

  var MAX_VISIBLE = 3;
  var DEFAULT_MS = 2600;

  var TYPE_ALIAS = {
    success: 'success', ok: 'success', done: 'success',
    error: 'error', err: 'error', fail: 'error', danger: 'error',
    warning: 'warning', warn: 'warning',
    info: 'info', primary: 'info', default: 'info'
  };

  var ICON = {
    success: '✓',
    error: '✕',
    warning: '!',
    info: 'i'
  };

  var host = null;          // .xjui-toast-stack
  var entries = [];         // 按创建顺序，最老在前
  var byKey = {};           // key -> entry

  function ensureHost() {
    if (host && document.body.contains(host)) return host;
    host = document.querySelector('.xjui-toast-stack');
    if (!host) {
      host = document.createElement('div');
      host.className = 'xjui-toast-stack';
      host.setAttribute('role', 'status');
      host.setAttribute('aria-live', 'polite');
      host.setAttribute('aria-atomic', 'false');
      document.body.appendChild(host);
    }
    return host;
  }

  function normType(t) {
    var k = String(t || 'info').toLowerCase();
    return TYPE_ALIAS[k] || 'info';
  }

  function removeEntry(entry, instant) {
    if (!entry || entry.dead) return;
    entry.dead = true;
    stopTimer(entry);
    if (byKey[entry.key] === entry) delete byKey[entry.key];
    var i = entries.indexOf(entry);
    if (i >= 0) entries.splice(i, 1);
    var node = entry.node;
    if (!node) return;
    if (instant) {
      if (node.parentNode) node.parentNode.removeChild(node);
      return;
    }
    node.classList.remove('xjui-in');
    node.classList.add('xjui-out');
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 200);
  }

  function stopTimer(entry) {
    if (entry.timer) { clearTimeout(entry.timer); entry.timer = null; }
    if (entry.barRaf) { cancelAnimationFrame(entry.barRaf); entry.barRaf = null; }
  }

  function startBar(entry, ms) {
    var bar = entry.bar;
    if (!bar) return;
    if (!ms) { bar.style.display = 'none'; return; }
    bar.style.display = '';
    bar.style.transition = 'none';
    bar.style.transform = 'scaleX(1)';
    // 强制回流后再启动过渡，否则浏览器会合并掉这两帧
    void bar.offsetWidth;
    bar.style.transition = 'transform ' + ms + 'ms linear';
    bar.style.transform = 'scaleX(0)';
  }

  function armTimer(entry, ms) {
    stopTimer(entry);
    entry.duration = ms;
    if (!ms) {
      if (entry.bar) entry.bar.style.display = 'none';
      return;
    }
    startBar(entry, ms);
    entry.startedAt = Date.now();
    entry.timer = setTimeout(function () { removeEntry(entry); }, ms);
  }

  function pause(entry) {
    if (!entry.timer) return;
    clearTimeout(entry.timer);
    entry.timer = null;
    entry.remaining = Math.max(600, entry.duration - (Date.now() - entry.startedAt));
    if (entry.bar) {
      var m = global.getComputedStyle(entry.bar).transform;
      entry.bar.style.transition = 'none';
      entry.bar.style.transform = m;
    }
  }

  function resume(entry) {
    if (entry.dead || entry.timer || !entry.remaining) return;
    var ms = entry.remaining;
    entry.remaining = 0;
    if (entry.bar) {
      entry.bar.style.transition = 'transform ' + ms + 'ms linear';
      entry.bar.style.transform = 'scaleX(0)';
    }
    entry.startedAt = Date.now();
    entry.duration = ms;
    entry.timer = setTimeout(function () { removeEntry(entry); }, ms);
  }

  function makeButton(label, kind, onClick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'xjui-toast-btn' + (kind ? ' xjui-' + kind : '');
    b.textContent = label;
    b.addEventListener('click', function (ev) {
      ev.stopPropagation();
      try { onClick(); } catch (e) { console.error('[toast] action failed', e); }
    });
    return b;
  }

  /**
   * 主入口。
   * @returns {{dismiss:Function, update:Function, node:HTMLElement}}
   */
  UI.toast = function (msg, type, options) {
    options = options || {};
    var kind = normType(type);
    var text = (msg === undefined || msg === null) ? '' : String(msg);
    var key = options.key || (kind + '\u0001' + text);

    // ---- 同内容去重：刷新倒计时 + 计数 ----
    var dup = byKey[key];
    if (dup && !dup.dead) {
      dup.count += 1;
      if (dup.countEl) {
        dup.countEl.textContent = '×' + dup.count;
        dup.countEl.style.display = '';
      }
      if (dup.msgEl) dup.msgEl.textContent = text;
      dup.node.classList.remove('xjui-pulse');
      void dup.node.offsetWidth;
      dup.node.classList.add('xjui-pulse');
      dup.remaining = 0;
      armTimer(dup, options.duration === undefined
        ? (options.sticky ? 0 : DEFAULT_MS)
        : Number(options.duration));
      // 重复时把这条挪到最下面（最新）
      if (host && dup.node.parentNode === host) host.appendChild(dup.node);
      return dup.handle;
    }

    // ---- 超出上限：挤掉最老的 ----
    while (entries.length >= MAX_VISIBLE) {
      removeEntry(entries[0], true);
    }

    var h = ensureHost();
    var node = document.createElement('div');
    node.className = 'xjui-toast xjui-toast-' + kind;
    node.setAttribute('role', kind === 'error' ? 'alert' : 'status');

    var icon = document.createElement('span');
    icon.className = 'xjui-toast-icon';
    icon.textContent = ICON[kind] || ICON.info;
    icon.setAttribute('aria-hidden', 'true');
    node.appendChild(icon);

    var main = document.createElement('div');
    main.className = 'xjui-toast-main';
    var msgEl = document.createElement('span');
    msgEl.className = 'xjui-toast-msg';
    msgEl.textContent = text;
    main.appendChild(msgEl);

    // ---- 操作按钮 ----
    var acts = [];
    if (typeof options.onRetry === 'function') {
      acts.push({ label: UI.t('toast.retry'), kind: 'primary', run: options.onRetry });
    }
    if (typeof options.onDetails === 'function') {
      acts.push({ label: UI.t('toast.details'), kind: '', run: options.onDetails });
    }
    if (typeof options.undoCallback === 'function') {
      acts.push({ label: UI.t('toast.undo'), kind: '', run: options.undoCallback });
    }
    if (Array.isArray(options.actions)) {
      options.actions.forEach(function (a) {
        if (a && a.label && typeof a.onClick === 'function') {
          acts.push({ label: a.label, kind: a.kind || '', run: a.onClick });
        }
      });
    }

    var actWrap = null;
    if (acts.length) {
      actWrap = document.createElement('div');
      actWrap.className = 'xjui-toast-acts';
      acts.forEach(function (a) {
        actWrap.appendChild(makeButton(a.label, a.kind, function () {
          a.run();
          removeEntry(entry);
        }));
      });
      main.appendChild(actWrap);
    }

    node.appendChild(main);

    var countEl = document.createElement('span');
    countEl.className = 'xjui-toast-count';
    countEl.textContent = '×2';
    countEl.style.display = 'none';
    node.appendChild(countEl);

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'xjui-toast-x';
    closeBtn.title = UI.t('toast.dismiss');
    closeBtn.setAttribute('aria-label', UI.t('toast.dismiss'));
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      removeEntry(entry);
    });
    node.appendChild(closeBtn);

    var bar = document.createElement('div');
    bar.className = 'xjui-toast-bar';
    node.appendChild(bar);

    var entry = {
      key: key,
      kind: kind,
      node: node,
      msgEl: msgEl,
      countEl: countEl,
      bar: bar,
      count: 1,
      timer: null,
      duration: 0,
      startedAt: 0,
      remaining: 0,
      dead: false
    };

    entry.handle = {
      node: node,
      dismiss: function () { removeEntry(entry); },
      update: function (newMsg, newType) {
        var t2 = newType ? normType(newType) : entry.kind;
        if (newType && t2 !== entry.kind) {
          node.classList.remove('xjui-toast-' + entry.kind);
          node.classList.add('xjui-toast-' + t2);
          icon.textContent = ICON[t2] || ICON.info;
          entry.kind = t2;
        }
        msgEl.textContent = String(newMsg === undefined ? '' : newMsg);
        entry.remaining = 0;
        armTimer(entry, options.duration === undefined
          ? (options.sticky ? 0 : DEFAULT_MS)
          : Number(options.duration));
      }
    };

    byKey[key] = entry;
    entries.push(entry);
    h.appendChild(node);

    // 入场（下一帧加类，保证过渡生效）
    requestAnimationFrame(function () { node.classList.add('xjui-in'); });

    var ms = options.duration === undefined
      ? (options.sticky ? 0 : DEFAULT_MS)
      : Number(options.duration);
    armTimer(entry, ms > 0 ? ms : 0);

    // 鼠标停在上面时暂停倒计时，方便点「查看详情」
    node.addEventListener('mouseenter', function () { pause(entry); });
    node.addEventListener('mouseleave', function () { resume(entry); });

    return entry.handle;
  };

  /* 快捷方法：UI.toast.ok('已保存') 之类 */
  ['success', 'error', 'warning', 'info'].forEach(function (k) {
    UI.toast[k] = function (msg, options) { return UI.toast(msg, k, options); };
  });

  /** 立刻清空所有提示 */
  UI.toast.clear = function () {
    entries.slice().forEach(function (e) { removeEntry(e, true); });
  };

  /** 当前可见条数（自测用） */
  UI.toast.count = function () { return entries.length; };

})(window);
