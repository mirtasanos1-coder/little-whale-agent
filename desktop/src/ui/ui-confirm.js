/* ============================================================================
   小鲸鱼 · 阻断式警告 / 输入框（V1.7.0）
   ----------------------------------------------------------------------------
   UI.showConfirm(title, body, options) -> Promise<boolean>
     options:
       okText      : 确认按钮文案。**必须是明确动作**（删除/清空/覆盖/允许执行…）
       cancelText  : 取消按钮文案。**不得是「取消」「确定」这类模糊词**
       danger      : 是否危险操作（默认 true）→ 按钮实心红
       risk        : 风险后果正文（必填语义；不传则用内置兜底文案）
       note        : 补充说明（灰色小字）
       details     : 长文本，可滚动区（可选）
       countdown   : 秒数，倒计时结束前确认键不可点（危险操作建议 5-10 秒）
       icon        : 标题前图标字符
       initialFocus: 'cancel'（默认）| 'ok'
       onOk/onCancel : 回调（Promise 仍会 resolve）

   UI.prompt(title, body, options) -> Promise<string|null>
     options: value, placeholder, okText, cancelText, required, validate,
              multiline, risk, note, maxlength

   规范强制点：
     · 必须阻断：点遮罩不关闭、Tab 锁在卡片内、背景滚动锁死
     · 默认焦点在「安全」那颗按钮（cancel）
     · Esc 等同取消
     · 危险按钮红色；按钮文案禁止「确定 / 取消」
   ========================================================================== */
(function (global) {
  'use strict';

  var UI = global.UI;
  if (!UI || !UI.__core_ready) return;
  if (UI.__confirm_ready) return;
  UI.__confirm_ready = true;

  /* 规范禁止的模糊文案 */
  var BANNED = ['确定', '确认', 'ok', 'okay', 'yes', '是', '好的', '好', '取消', 'cancel', '关闭', 'close'];

  function isBanned(label) {
    var s = String(label == null ? '' : label).trim();
    if (!s) return true;
    return BANNED.indexOf(s.toLowerCase()) >= 0;
  }

  /** 把模糊文案换成明确动作 */
  function safeOkText(label, actionName) {
    if (!isBanned(label)) return String(label).trim();
    return '确认' + (actionName || '执行');
  }
  function safeCancelText(label, actionName) {
    if (!isBanned(label)) return String(label).trim();
    if (actionName) return '暂不' + actionName;
    return '暂不执行';
  }

  /* ------------------------------------------------------------------ *
   * 公共骨架：造一个模态卡片
   * ------------------------------------------------------------------ */
  function buildShell(opt) {
    var mask = document.createElement('div');
    mask.className = 'xjui-mask xjui-z-confirm';

    var card = document.createElement('div');
    card.className = 'xjui-card' + (opt.wide ? ' xjui-wide' : '');
    mask.appendChild(card);

    // --- 标题 ---
    var head = document.createElement('div');
    head.className = 'xjui-head';
    var icon = document.createElement('span');
    icon.className = 'xjui-title-icon ' + (opt.danger ? 'xjui-t-danger' : 'xjui-t-info');
    icon.textContent = opt.icon || (opt.danger ? '⚠' : 'ℹ');
    icon.setAttribute('aria-hidden', 'true');
    head.appendChild(icon);
    var h = document.createElement('h3');
    h.className = 'xjui-title';
    h.id = 'xjui-title-' + (++UID);
    h.textContent = opt.title || UI.t('ui.confirm');
    head.appendChild(h);
    card.appendChild(head);
    mask.setAttribute('aria-labelledby', h.id);

    // --- 正文 ---
    var bodyWrap = document.createElement('div');
    bodyWrap.className = 'xjui-body';
    card.appendChild(bodyWrap);

    if (opt.body) {
      var p = document.createElement('p');
      p.className = 'xjui-text';
      p.textContent = String(opt.body);
      bodyWrap.appendChild(p);
    }

    if (opt.details) {
      var d = document.createElement('div');
      d.className = 'xjui-sign-scroll';
      d.style.marginTop = '10px';
      var dt = document.createElement('p');
      dt.className = 'xjui-sign-text';
      dt.textContent = String(opt.details);
      d.appendChild(dt);
      bodyWrap.appendChild(d);
    }

    // --- 风险后果（永远渲染，规范要求） ---
    var riskBox = document.createElement('div');
    riskBox.className = 'xjui-risk';
    var cap = document.createElement('span');
    cap.className = 'xjui-risk-cap';
    cap.textContent = UI.t('confirm.risk');
    riskBox.appendChild(cap);
    var rp = document.createElement('p');
    rp.className = 'xjui-risk-text';
    rp.textContent = String(opt.risk || UI.t('confirm.default_risk'));
    riskBox.appendChild(rp);
    bodyWrap.appendChild(riskBox);

    if (opt.note) {
      var note = document.createElement('div');
      note.className = 'xjui-note';
      note.textContent = String(opt.note);
      bodyWrap.appendChild(note);
    }

    return { mask: mask, card: card, bodyWrap: bodyWrap };
  }

  var UID = 0;

  /* ------------------------------------------------------------------ *
   * UI.showConfirm
   * ------------------------------------------------------------------ */
  UI.showConfirm = function (title, body, options) {
    options = options || {};
    var danger = options.danger !== false;
    var actionName = options.actionName || '';

    var okText = safeOkText(options.okText, actionName);
    var cancelText = safeCancelText(options.cancelText, actionName);

    if (options.okText && isBanned(options.okText)) {
      try { console.warn('[UI.showConfirm] 按钮文案「' + options.okText + '」太模糊，已改为「' + okText + '」'); } catch (e) {}
    }

    var shell = buildShell({
      title: title,
      body: body,
      risk: options.risk,
      note: options.note,
      details: options.details,
      danger: danger,
      icon: options.icon,
      wide: !!options.wide
    });

    var foot = document.createElement('div');
    foot.className = 'xjui-foot';

    var btnCancel = document.createElement('button');
    btnCancel.type = 'button';
    btnCancel.className = 'xjui-btn';
    btnCancel.textContent = cancelText;

    var btnOk = document.createElement('button');
    btnOk.type = 'button';
    btnOk.className = 'xjui-btn ' + (danger ? 'xjui-danger' : 'xjui-primary');
    btnOk.textContent = okText;

    //纯提示框（例如「知道了」）只留一颗按钮，避免出现「暂不知道了」这种文案
    var single = !!options.single;
    if (!single) foot.appendChild(btnCancel);
    foot.appendChild(btnOk);
    shell.card.appendChild(foot);

    return new Promise(function (resolve) {
      var settled = false;
      var layer = null;
      var cdTimer = null;
      var left = Number(options.countdown) || 0;

      function finish(v) {
        if (settled) return;
        settled = true;
        if (cdTimer) clearInterval(cdTimer);
        UI.closeLayer(layer);
        setTimeout(function () {
          if (shell.mask.parentNode) shell.mask.parentNode.removeChild(shell.mask);
        }, 0);
        try {
          if (v && typeof options.onOk === 'function') options.onOk();
          if (!v && typeof options.onCancel === 'function') options.onCancel();
        } catch (e) { console.error('[UI.showConfirm] callback failed', e); }
        resolve(!!v);
      }

      if (left > 0) {
        btnOk.setAttribute('aria-disabled', 'true');
        btnOk.textContent = okText + '（' + left + 's）';
        cdTimer = setInterval(function () {
          left -= 1;
          if (left <= 0) {
            clearInterval(cdTimer);
            cdTimer = null;
            btnOk.removeAttribute('aria-disabled');
            btnOk.textContent = okText;
            return;
          }
          btnOk.textContent = okText + '（' + left + 's）';
        }, 1000);
      }

      btnOk.addEventListener('click', function () {
        if (btnOk.getAttribute('aria-disabled') === 'true') {
          btnOk.classList.remove('xjui-shake');
          void btnOk.offsetWidth;
          btnOk.classList.add('xjui-shake');
          return;
        }
        finish(true);
      });
      btnCancel.addEventListener('click', function () { finish(false); });

      // 点遮罩不关闭（强阻断）；需要时可用 dismissOnBackdrop 打开
      shell.mask.addEventListener('mousedown', function (ev) {
        if (ev.target !== shell.mask) return;
        if (options.dismissOnBackdrop) { finish(false); return; }
        shell.card.classList.remove('xjui-shake');
        void shell.card.offsetWidth;
        shell.card.classList.add('xjui-shake');
      });

      document.body.appendChild(shell.mask);
      layer = UI.openLayer(shell.mask, {
        onEsc: function () { finish(false); },
        initialFocus: (single || options.initialFocus === 'ok') ? btnOk : btnCancel
      });
    });
  };

  /* 别名：语义更贴近原 confirmDialog */
  UI.confirm = UI.showConfirm;

  /* ------------------------------------------------------------------ *
   * UI.prompt
   * ------------------------------------------------------------------ */
  UI.prompt = function (title, body, options) {
    options = options || {};
    var actionName = options.actionName || '保存';
    var okText = safeOkText(options.okText || '保存', actionName);
    var cancelText = safeCancelText(options.cancelText, actionName);

    var shell = buildShell({
      title: title,
      body: body,
      risk: options.risk || '（无风险，仅修改本地设置）',
      note: options.note,
      danger: false,
      icon: options.icon || '✎'
    });

    var inputWrap = document.createElement('div');
    inputWrap.style.marginTop = '10px';

    var input;
    if (options.multiline) {
      input = document.createElement('textarea');
      input.rows = 4;
      input.className = 'xjui-input';
      input.style.resize = 'vertical';
    } else {
      input = document.createElement('input');
      input.type = options.inputType || 'text';
      input.className = 'xjui-input';
    }
    if (options.placeholder) input.placeholder = String(options.placeholder);
    if (options.maxlength) input.maxLength = Number(options.maxlength);
    input.value = options.value == null ? '' : String(options.value);
    inputWrap.appendChild(input);
    shell.bodyWrap.appendChild(inputWrap);

    var hint = document.createElement('div');
    hint.className = 'xjui-sign-hint';
    shell.bodyWrap.appendChild(hint);

    var foot = document.createElement('div');
    foot.className = 'xjui-foot';
    var btnCancel = document.createElement('button');
    btnCancel.type = 'button';
    btnCancel.className = 'xjui-btn';
    btnCancel.textContent = cancelText;
    var btnOk = document.createElement('button');
    btnOk.type = 'button';
    btnOk.className = 'xjui-btn xjui-primary';
    btnOk.textContent = okText;
    foot.appendChild(btnCancel);
    foot.appendChild(btnOk);
    shell.card.appendChild(foot);

    return new Promise(function (resolve) {
      var settled = false;
      var layer = null;

      function finish(v) {
        if (settled) return;
        settled = true;
        UI.closeLayer(layer);
        setTimeout(function () {
          if (shell.mask.parentNode) shell.mask.parentNode.removeChild(shell.mask);
        }, 0);
        resolve(v);
      }

      function submit() {
        var v = input.value;
        if (options.required && !String(v).trim()) {
          hint.textContent = UI.t('prompt.need_value');
          hint.classList.add('xjui-hint-err');
          shell.card.classList.remove('xjui-shake');
          void shell.card.offsetWidth;
          shell.card.classList.add('xjui-shake');
          input.focus();
          return;
        }
        if (typeof options.validate === 'function') {
          var bad = options.validate(v);
          if (bad) {
            hint.textContent = String(bad);
            hint.classList.add('xjui-hint-err');
            input.focus();
            return;
          }
        }
        finish(String(v));
      }

      btnOk.addEventListener('click', submit);
      btnCancel.addEventListener('click', function () { finish(null); });
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' && !options.multiline) {
          ev.preventDefault();
          submit();
        }
      });

      document.body.appendChild(shell.mask);
      layer = UI.openLayer(shell.mask, {
        onEsc: function () { finish(null); },
        initialFocus: input
      });
      setTimeout(function () { try { input.focus(); input.select(); } catch (e) {} }, 40);
    });
  };

})(window);
