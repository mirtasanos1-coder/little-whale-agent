/* ============================================================================
   小鲸鱼 · 强模态签署（V1.7.0）
   ----------------------------------------------------------------------------
   UI.showSignDialog(title, content, options) -> Promise<{ok:true, id, at} | null>

   options:
     version      : 条款版本号（必填语义，默认取 options.version || '1.0.0'）
     agreement    : 协议标识（落库用，如 'disclaimer' / 'learn_bottom_line'）
     acceptText   : 确认按钮文案，默认「同意并继续」
     declineText  : 取消按钮文案，默认「暂不同意」
     agreeLabel   : 勾选文案，默认「我已阅读并同意上述内容」
     requireSign  : 是否要求签名（学习底线协议用）
     signLabel    : 签名标签，默认「签名」
     onAccepted   : 审计写库成功后的回调
     auditMethod  : 后端方法名，默认 'audit_sign_accept'
     extra        : 附带给后端的额外字段（对象）

   规范强制点：
     · z-index 99999，点遮罩不关闭，Tab 锁在卡片内，背景滚动锁死
     · 未勾选时确认键 aria-disabled，点击只抖一下并提示
     · 勾选 + （需签名时）签名合格 → 才可点
     · 点同意后**先落库**：失败则停在弹窗里提示可重试，绝不假装成功
   ========================================================================== */
(function (global) {
  'use strict';

  var UI = global.UI;
  if (!UI || !UI.__core_ready) return;
  if (UI.__sign_ready) return;
  UI.__sign_ready = true;

  var UID = 0;

  UI.showSignDialog = function (title, content, options) {
    options = options || {};

    var agreement = String(options.agreement || 'generic');
    var version = String(options.version || '1.0.0');
    var acceptText = String(options.acceptText || UI.t('sign.accept'));
    var declineText = String(options.declineText || UI.t('sign.decline'));
    var agreeLabel = String(options.agreeLabel || UI.t('sign.agree'));
    var requireSign = !!options.requireSign;
    var auditMethod = String(options.auditMethod || 'audit_sign_accept');
    var text = content == null ? '' : String(content);

    /* ---------- 骨架 ---------- */
    var mask = document.createElement('div');
    mask.className = 'xjui-mask xjui-z-sign';

    var card = document.createElement('div');
    card.className = 'xjui-card xjui-wide';
    mask.appendChild(card);

    var head = document.createElement('div');
    head.className = 'xjui-head';
    var icon = document.createElement('span');
    icon.className = 'xjui-title-icon xjui-t-info';
    icon.textContent = '📄';
    icon.setAttribute('aria-hidden', 'true');
    head.appendChild(icon);
    var h = document.createElement('h3');
    h.className = 'xjui-title';
    h.id = 'xjui-sign-title-' + (++UID);
    h.textContent = title || UI.t('sign.title');
    head.appendChild(h);
    card.appendChild(head);
    mask.setAttribute('aria-labelledby', h.id);

    var bodyWrap = document.createElement('div');
    bodyWrap.className = 'xjui-body';
    card.appendChild(bodyWrap);

    // 条款滚动区
    var scroll = document.createElement('div');
    scroll.className = 'xjui-sign-scroll';
    scroll.tabIndex = 0;            // 让键盘用户也能滚动阅读
    var pre = document.createElement('p');
    pre.className = 'xjui-sign-text';
    pre.textContent = text;
    scroll.appendChild(pre);
    bodyWrap.appendChild(scroll);

    // 展开/折叠（仅在内容超出时出现）
    var moreWrap = document.createElement('div');
    moreWrap.className = 'xjui-sign-more';
    moreWrap.style.display = 'none';
    var moreBtn = document.createElement('button');
    moreBtn.type = 'button';
    moreBtn.className = 'xjui-linkbtn';
    moreBtn.textContent = UI.t('sign.expand');
    moreWrap.appendChild(moreBtn);
    bodyWrap.appendChild(moreWrap);

    var expanded = false;
    moreBtn.addEventListener('click', function () {
      expanded = !expanded;
      scroll.classList.toggle('xjui-expanded', expanded);
      moreBtn.textContent = expanded ? UI.t('sign.collapse') : UI.t('sign.expand');
      if (expanded) scroll.scrollTop = scroll.scrollHeight;
    });

    // 勾选框
    var gate = document.createElement('label');
    gate.className = 'xjui-sign-gate';
    var ck = document.createElement('input');
    ck.type = 'checkbox';
    ck.id = 'xjui-sign-ck-' + UID;
    var ckText = document.createElement('span');
    ckText.textContent = agreeLabel;
    gate.appendChild(ck);
    gate.appendChild(ckText);
    bodyWrap.appendChild(gate);

    // 可选签名
    var signInput = null;
    if (requireSign) {
      var signWrap = document.createElement('div');
      signWrap.className = 'xjui-sign-sign';
      var sl = document.createElement('label');
      sl.textContent = UI.t('sign.sign_label');
      sl.htmlFor = 'xjui-sign-input-' + UID;
      signInput = document.createElement('input');
      signInput.type = 'text';
      signInput.id = 'xjui-sign-input-' + UID;
      signInput.className = 'xjui-input';
      signInput.placeholder = UI.t('sign.sign_ph');
      signInput.maxLength = 40;
      signWrap.appendChild(sl);
      signWrap.appendChild(signInput);
      bodyWrap.appendChild(signWrap);
    }

    // 提示行
    var hint = document.createElement('div');
    hint.className = 'xjui-sign-hint';
    hint.textContent = UI.t('sign.esc_hint');
    bodyWrap.appendChild(hint);

    // 按钮
    var foot = document.createElement('div');
    foot.className = 'xjui-foot';
    var btnDecline = document.createElement('button');
    btnDecline.type = 'button';
    btnDecline.className = 'xjui-btn';
    btnDecline.textContent = declineText;
    var btnAccept = document.createElement('button');
    btnAccept.type = 'button';
    btnAccept.className = 'xjui-btn xjui-primary';
    btnAccept.textContent = acceptText;
    btnAccept.setAttribute('aria-disabled', 'true');
    foot.appendChild(btnDecline);
    foot.appendChild(btnAccept);
    card.appendChild(foot);

    return new Promise(function (resolve) {
      var settled = false;
      var layer = null;
      var busy = false;

      function setHint(msg, isErr) {
        hint.textContent = msg || '';
        hint.classList.toggle('xjui-hint-err', !!isErr);
      }

      function ready() {
        if (!ck.checked) return false;
        if (requireSign && signInput && String(signInput.value).trim().length < 2) return false;
        return true;
      }

      function syncGate() {
        if (busy) return;
        var ok = ready();
        if (ok) {
          btnAccept.removeAttribute('aria-disabled');
          setHint(UI.t('sign.esc_hint'), false);
        } else {
          btnAccept.setAttribute('aria-disabled', 'true');
          if (!ck.checked) setHint(UI.t('sign.esc_hint'), false);
        }
      }

      function finish(v) {
        if (settled) return;
        settled = true;
        UI.closeLayer(layer);
        setTimeout(function () {
          if (mask.parentNode) mask.parentNode.removeChild(mask);
        }, 0);
        resolve(v);
      }

      function shake() {
        card.classList.remove('xjui-shake');
        void card.offsetWidth;
        card.classList.add('xjui-shake');
      }

      /* -------- 核心：先落库，再算同意 -------- */
      function doAccept() {
        if (busy) return;
        if (!ck.checked) {
          shake();
          setHint(UI.t('sign.need_agree'), true);
          ck.focus();
          return;
        }
        if (requireSign && signInput && String(signInput.value).trim().length < 2) {
          shake();
          setHint(UI.t('sign.need_sign'), true);
          signInput.focus();
          return;
        }

        busy = true;
        btnAccept.setAttribute('aria-disabled', 'true');
        btnDecline.setAttribute('aria-disabled', 'true');
        btnAccept.textContent = UI.t('sign.audit_saving');
        setHint(UI.t('sign.audit_saving'), false);

        var payload = {
          agreement: agreement,
          version: version,
          title: String(title || ''),
          accepted_at: new Date().toISOString(),
          signature: signInput ? String(signInput.value).trim() : '',
          client: {
            ua: (global.navigator && global.navigator.userAgent) || '',
            lang: (global.navigator && global.navigator.language) || '',
            tz: (function () {
              try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { return ''; }
            })(),
            screen: (global.screen ? (global.screen.width + 'x' + global.screen.height) : '')
          }
        };
        if (options.extra && typeof options.extra === 'object') {
          for (var k in options.extra) {
            if (Object.prototype.hasOwnProperty.call(options.extra, k)) payload[k] = options.extra[k];
          }
        }

        UI.rpc(auditMethod, [payload]).then(function (r) {
          var ok = r && (r.ok === true || r.ok === 1 || r.ok === 'true');
          if (!ok) {
            busy = false;
            btnDecline.removeAttribute('aria-disabled');
            btnAccept.removeAttribute('aria-disabled');
            btnAccept.textContent = acceptText;
            setHint(UI.t('sign.audit_failed') + (r && r.msg ? '（' + r.msg + '）' : ''), true);
            // 失败时把「同意」变成「重试保存并同意」，让用户能再点一次
            btnAccept.textContent = UI.t('sign.audit_retry');
            return;
          }
          var at = (r && (r.at || r.time || r.ts)) || payload.accepted_at;
          var id = (r && (r.id || r.record_id)) || 0;
          try {
            if (typeof options.onAccepted === 'function') options.onAccepted(r);
          } catch (e) { console.error('[UI.showSignDialog] onAccepted failed', e); }
          finish({ ok: true, id: id, at: at, agreement: agreement, version: version,
                   signature: payload.signature, raw: r });
        });
      }

      btnAccept.addEventListener('click', function () {
        if (btnAccept.getAttribute('aria-disabled') === 'true' && !busy) {
          doAccept();     // 让 doAccept 去决定是「提示勾选」还是「重试保存」
          return;
        }
        doAccept();
      });
      btnDecline.addEventListener('click', function () {
        if (busy) return;
        finish(null);
      });

      ck.addEventListener('change', syncGate);
      if (signInput) signInput.addEventListener('input', syncGate);

      // 点遮罩不关闭（强模态）
      mask.addEventListener('mousedown', function (ev) {
        if (ev.target !== mask) return;
        shake();
      });

      document.body.appendChild(mask);
      layer = UI.openLayer(mask, {
        onEsc: function () { if (!busy) finish(null); },
        initialFocus: ck
      });

      // 挂载后判断是否需要「展开全文」
      setTimeout(function () {
        if (UI.isOverflowing(scroll)) {
          moreWrap.style.display = 'flex';
        } else {
          moreWrap.style.display = 'none';
        }
      }, 60);
    });
  };

  /* 便捷判断：这个协议版本是不是已经签过 */
  UI.hasSigned = function (agreement, version) {
    return UI.rpc('audit_sign_state', [agreement]).then(function (r) {
      if (!r || !r.ok) return false;
      if (!version) return !!r.signed;
      return String(r.version || '') === String(version);
    });
  };

})(window);
