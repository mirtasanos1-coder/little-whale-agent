/* ============================================================================
   小鲸鱼 · 统一交互组件底座（V1.7.0）
   ----------------------------------------------------------------------------
   这个文件只做四件事，其余组件（ui-toast / ui-confirm / ui-sign）都挂在它上面：
     1) window.UI 命名空间 + 文案（i18n）查找
     2) 模态层栈：z-index / 焦点锁定 / Esc 逐层关闭 / 背景滚动锁
     3) 一点点 DOM 构造工具（不依赖 index.html 里的 $ 与 Tailwind 类）
     4) 与后端通信的钩子（UI._rpc，默认走 window.call / __bridge.call）

   设计约束（来自需求）：
     · 全部配色只用 index.html :root 的主题变量，禁止硬编码 → 亮/暗主题自动适配
     · 全部文案先取中文原文，再经 window.UI_MAP（中文原文→译文）翻译 → 复用既有 i18n
     · 模态打开时 Tab 不得跑进背景；Esc 只关最上面那一层
   ========================================================================== */
(function (global) {
  'use strict';

  var UI = global.UI = global.UI || {};
  if (UI.__core_ready) return;

  UI.version = '1.7.0';
  UI.__core_ready = true;

  /* ------------------------------------------------------------------ *
   * 一、文案表：key -> 中文原文
   * 中文原文同时也是 i18n 的查找键（后端 ui_dict() 给的就是「中文原文:译文」）
   * ------------------------------------------------------------------ */
  var TEXT = {
    'ui.cancel': '取消',
    'ui.confirm': '确定',
    'ui.close': '关闭',

    'toast.retry': '重试',
    'toast.details': '查看详情',
    'toast.undo': '撤销',
    'toast.dismiss': '关闭这条提示',

    'confirm.risk': '风险后果',
    'confirm.default_risk': '这一步执行后无法自动还原，请确认你已看清上面的后果。',

    'sign.title': '请阅读并同意',
    'sign.agree': '我已阅读并同意上述内容',
    'sign.accept': '同意并继续',
    'sign.decline': '暂不同意',
    'sign.expand': '展开全文',
    'sign.collapse': '收起全文',
    'sign.sign_label': '签名',
    'sign.sign_ph': '在这里写下你的名字',
    'sign.need_agree': '请先勾选「我已阅读并同意」',
    'sign.need_sign': '请先填写签名（至少 2 个字）',
    'sign.audit_saving': '正在保存签署记录…',
    'sign.audit_failed': '签署记录没能保存，请重试',
    'sign.audit_retry': '重试保存并同意',
    'sign.audit_ok': '已记录',
    'sign.esc_hint': '按 Esc 等同「暂不同意」',

    'prompt.title': '输入',
    'prompt.ok': '保存',
    'prompt.need_value': '这一项不能为空'
  };

  /**
   * 取一条文案并翻译。
   * @param {string} key TEXT 里的键；也可以直接传中文原文。
   * @param {object} [vars] 替换文案里的 {name}
   */
  UI.t = function (key, vars) {
    var s = TEXT[key];
    if (s === undefined) s = key;
    var map = global.UI_MAP;
    if (map && typeof map === 'object') {
      var hit = map[s];
      if (typeof hit === 'string' && hit) s = hit;
    }
    if (vars) {
      s = String(s).replace(/\{(\w+)\}/g, function (m, k) {
        return (vars[k] === undefined || vars[k] === null) ? m : String(vars[k]);
      });
    }
    return s;
  };

  /* ------------------------------------------------------------------ *
   * 二、DOM 工具
   * ------------------------------------------------------------------ */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  UI.el = el;

  function clear(n) {
    while (n && n.firstChild) n.removeChild(n.firstChild);
    return n;
  }
  UI.clear = clear;

  /** 只在「内容真的超出容器」时才需要展开/收起 */
  function isOverflowing(node) {
    if (!node) return false;
    return node.scrollHeight - node.clientHeight > 6;
  }
  UI.isOverflowing = isOverflowing;

  /* ------------------------------------------------------------------ *
   * 三、模态层栈
   * ------------------------------------------------------------------ */
  var stack = [];          // [{el, onEsc, onTab, restoreFocus, prevOverflow}]
  var bound = false;

  function focusables(root) {
    if (!root) return [];
    var sel = 'a[href],area[href],input,select,textarea,button,[tabindex]';
    var list = Array.prototype.slice.call(root.querySelectorAll(sel));
    return list.filter(function (n) {
      if (n.disabled) return false;
      if (n.getAttribute('tabindex') === '-1') return false;
      if (n.getAttribute('type') === 'hidden') return false;
      if (n.getAttribute('aria-hidden') === 'true') return false;
      if (typeof n.getClientRects === 'function' && n.getClientRects().length === 0) return false;
      return true;
    });
  }

  function cycleTab(root, ev) {
    var list = focusables(root);
    if (!list.length) { ev.preventDefault(); return; }
    var first = list[0];
    var last = list[list.length - 1];
    var act = document.activeElement;
    var inside = root.contains(act);
    if (!inside) {
      ev.preventDefault();
      (ev.shiftKey ? last : first).focus();
      return;
    }
    if (ev.shiftKey && act === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && act === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  function onKeyDown(ev) {
    var top = stack[stack.length - 1];
    if (!top) return;
    if (ev.key === 'Escape' || ev.key === 'Esc') {
      if (top.onEsc) {
        ev.preventDefault();
        ev.stopPropagation();
        top.onEsc();
      }
      return;
    }
    if (ev.key === 'Tab') cycleTab(top.el, ev);
  }

  /** 焦点被鼠标点到背景时，强行拉回最上层模态 */
  function onFocusIn(ev) {
    var top = stack[stack.length - 1];
    if (!top) return;
    if (top.el.contains(ev.target)) {
      top.el.__xjuiLastFocus = ev.target;   // 记住最后停在哪，掉焦点时能回去
      return;
    }
    var list = focusables(top.el);
    if (list.length) list[0].focus();
  }

  function bindGlobal() {
    if (bound) return;
    bound = true;
    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('focusin', onFocusIn, true);
    // 焦点被扔到 body（例如点到不可聚焦的遮罩）时，重新拉回弹窗
    document.addEventListener('focusout', function (ev) {
      var top = stack[stack.length - 1];
      if (!top) return;
      if (ev.relatedTarget) return;
      setTimeout(function () {
        var t = stack[stack.length - 1];
        if (t !== top) return;
        if (t.el.contains(document.activeElement)) return;
        var list = focusables(t.el);
        if (list.length) {
          var last = t.el.__xjuiLastFocus;
          var back = (last && t.el.contains(last)) ? last : list[0];
          try { back.focus(); } catch (e) { /* ignore */ }
        }
      }, 0);
    }, true);
  }

  /**
   * 打开一层模态。
   * @param {HTMLElement} maskEl 遮罩元素（必须是 position:fixed; inset:0）
   * @param {object} opts {onEsc, zClass, initialFocus}
   */
  UI.openLayer = function (maskEl, opts) {
    opts = opts || {};
    bindGlobal();
    if (stack.length === 0) {
      document.documentElement.style.overflow = 'hidden';
    }
    maskEl.classList.add('xjui-open');
    var entry = {
      el: maskEl,
      onEsc: opts.onEsc || null,
      restoreFocus: document.activeElement,
      prevOverflow: document.documentElement.style.overflow
    };
    stack.push(entry);
    // 让读屏软件知道后面那层是背景
    maskEl.setAttribute('role', 'dialog');
    maskEl.setAttribute('aria-modal', 'true');
    // 点遮罩空白处时不要让焦点掉到 body（遮罩本身不可聚焦，默认会丢焦点）
    if (!maskEl.__xjuiNoFocusLoss) {
      maskEl.__xjuiNoFocusLoss = true;
      maskEl.addEventListener('mousedown', function (ev) {
        if (ev.target === maskEl) ev.preventDefault();
      }, true);
    }
    setTimeout(function () {
      var t = opts.initialFocus;
      if (!t) {
        var list = focusables(maskEl);
        // 默认焦点给「安全」的那颗按钮：调用方通过 initialFocus 传，否则取最后一个
        t = list.length ? list[list.length - 1] : maskEl;
      }
      try { t.focus(); } catch (e) { /* 元素可能已卸载 */ }
    }, 30);
    return entry;
  };

  /** 关闭最上层（或指定层）模态 */
  UI.closeLayer = function (entry) {
    if (!stack.length) return;
    var idx = entry ? stack.indexOf(entry) : stack.length - 1;
    if (idx < 0) return;
    var e = stack[idx];
    stack.splice(idx, 1);
    if (e.el) {
      e.el.classList.remove('xjui-open');
      e.el.removeAttribute('aria-modal');
    }
    if (stack.length === 0) {
      document.documentElement.style.overflow = e.prevOverflow || '';
    }
    var rf = e.restoreFocus;
    if (rf && typeof rf.focus === 'function' && document.documentElement.contains(rf)) {
      try { rf.focus(); } catch (err) { /* ignore */ }
    }
  };

  UI.layerDepth = function () { return stack.length; };
  UI.topLayer = function () { return stack.length ? stack[stack.length - 1] : null; };
  UI.isBlocking = function () { return stack.length > 0; };

  /* ------------------------------------------------------------------ *
   * 四、后端通信钩子
   * 默认按「window.call → __bridge.call」的顺序找，找到就用；
   * 测试环境可以直接改 UI._rpc。
   * ------------------------------------------------------------------ */
  UI._rpc = function (method, args) {
    args = args || [];
    // 优先走桥接层：首启动时后端可能还没把 API 赋给 window.call，
    // 而 __bridge 是独立通道（first-run.js 一直用它读写配置，最稳）。
    if (global.__bridge && typeof global.__bridge.call === 'function') {
      return Promise.resolve(global.__bridge.call(method, args));
    }
    if (typeof global.call === 'function') {
      return Promise.resolve(global.call.apply(null, [method].concat(args)));
    }
    return Promise.reject(new Error('no backend channel'));
  };

  /** 统一的「安全调用」：永不抛，失败返回 {ok:false, msg} */
  UI.rpc = function (method, args) {
    return new Promise(function (resolve) {
      var p;
      try {
        p = UI._rpc(method, args || []);
      } catch (e) {
        resolve({ ok: false, msg: (e && e.message) || String(e) });
        return;
      }
      Promise.resolve(p).then(function (r) {
        resolve(r && typeof r === 'object' ? r : { ok: false, msg: 'empty response' });
      }, function (e) {
        resolve({ ok: false, msg: (e && e.message) || String(e) });
      });
    });
  };

  /* ------------------------------------------------------------------ *
   * 五、主题
   * ------------------------------------------------------------------ */
  UI.isLightTheme = function () {
    var v = document.documentElement.getAttribute('data-theme-dark');
    return v === '0' || v === 'false';
  };

})(window);
