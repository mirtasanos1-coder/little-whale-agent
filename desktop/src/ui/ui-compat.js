/* ============================================================================
   小鲸鱼 · 旧接口兼容层（V1.7.0）
   ----------------------------------------------------------------------------
   目的：index.html 里已经有 466 处 toast()、52 处 confirmDialog()、5 处
   promptDialog()、2 处 riskConfirm()、4 处原生 confirm()，还有 first-run.js /
   setup-wizard.js / onboarding.js 也在调这些全局函数。

   与其逐处改写（风险大、容易漏），不如**保留同名全局函数、只换实现**：
   它们内部转调 window.UI.*，调用方一行都不用动。

   加载方式：必须放在 <head> 里用 defer，保证在本页内联脚本（4833 起那一大块）
   定义完旧函数之后才执行，否则会被覆盖回去。
   ========================================================================== */
(function (global) {
  'use strict';

  var UI = global.UI;
  if (!UI || !UI.__core_ready) {
    try { console.error('[ui-compat] UI 底座没加载，兼容层跳过'); } catch (e) {}
    return;
  }

  var LEGACY_TYPE = {
    ok: 'success',
    success: 'success',
    err: 'error',
    error: 'error',
    fail: 'error',
    warn: 'warning',
    warning: 'warning',
    info: 'info'
  };

  /** 从标题里捏一个明确动作（旧代码常把 okText 留成「确定」） */
  // v1.8.1：先把「确定卸载」「确认删除」「是否要清空」这类前缀剥掉。
  // 不剥的话取消按钮会拼成「暂不确定卸载」，读起来像病句。
  var VAGUE_HEAD = /^(?:确定|确认|是否要|是否|要不要|需要|要|请)(?=\S)/;
  function stripQuotes(s) {
    // 「小鲸鱼」这类书名号内容对按钮没意义，连壳一起丢掉
    return String(s || '').replace(/[「『【\[（(][^」』】\]）)]*[」』】\]）)]/g, '');
  }
  function stripVague(s) {
    var t = stripQuotes(s).replace(/\s+/g, '');
    var prev = null;
    while (t && t !== prev) {   // 可能连着好几层前缀，剥到不动为止
        prev = t;
        t = t.replace(VAGUE_HEAD, '');
    }
    return t;
  }

  function actionFromTitle(title) {
    var s = stripVague(String(title || '').replace(/[？?！!。…、,，\s]+$/g, '')).trim();
    if (!s) return '';
    // 「真的要设置这么多吗」这类没有动词的，就别拿来当按钮
    if (s.length > 14) s = s.slice(0, 14);
    return s;
  }

  /* ------------------------------------------------------------------ *
   * toast(msg, type) —— 旧签名原样支持
   * ------------------------------------------------------------------ */
  global.toast = function (msg, type, options) {
    var t = LEGACY_TYPE[String(type || 'info').toLowerCase()] || 'info';
    try {
      return UI.toast(msg, t, options);
    } catch (e) {
      try { console.log('[toast]', msg); } catch (e2) {}
    }
  };

  /* ------------------------------------------------------------------ *
   * confirmDialog(title, body, okText='确定', danger=true, note='')
   * 旧实现里 note 是红字，正好对应新的「风险后果」
   * ------------------------------------------------------------------ */
   // 「知道了」这类纯提示没有「取消」语义，用单按钮框
  var ACK = ['知道了', '明白', '收到', '了解', '好的', '好', '嗯'];

  var VAGUE = ['确定', '确认', 'ok', 'okay', 'yes', '是', '好的', '好', ''];

  global.confirmDialog = function (title, body, okText, danger, note) {
    var dangerFlag = danger === undefined ? true : !!danger;
    var ok = stripVague(String(okText == null ? '' : okText).trim());
    var derived = '';
    // 旧代码里还有几处把 okText 留成「确定」，这里用标题捏一个明确动作
    if (VAGUE.indexOf(ok.toLowerCase()) >= 0) {
      derived = actionFromTitle(title);
      if (derived) ok = derived;
    }
    var opt = {
      danger: dangerFlag,
      risk: note || '',
      note: '',
      okText: ok
    };
    // 取消按钮别写成「暂不立刻衰减一次」这种长句：
    // 确认按钮本身够短（≤8 字）就直接「暂不+确认按钮」，否则退回标题动作
    var shortOk = ok && ok.length <= 8;
    var base = shortOk ? ok : (derived || actionFromTitle(title));
    if (base) opt.cancelText = '暂不' + base;
    if (!opt.risk) delete opt.risk;
    if (ACK.indexOf(ok) >=0) {
      opt.single = true; //纯提示：只留「知道了」这一颗按钮
      delete opt.cancelText;
    }
    return UI.showConfirm(title, body, opt).then(function (v) {
      return v;
    });
  };

  /* ------------------------------------------------------------------ *
   * riskConfirm(title, body, okText, seconds, note) —— 带倒计时
   * ------------------------------------------------------------------ */
  global.riskConfirm = function (title, body, okText, seconds, note) {
    return UI.showConfirm(title, body, {
      danger: true,
      risk: note || '',
      okText: okText || '我已了解',
      countdown: Number(seconds) > 0 ? Number(seconds) : 0
    });
  };

  /* ------------------------------------------------------------------ *
   * promptDialog(title, body, defVal, okText) -> string | null
   * ------------------------------------------------------------------ */
  global.promptDialog = function (title, body, defVal, okText) {
    return UI.prompt(title, body, {
      value: defVal == null ? '' : defVal,
      okText: okText || '保存'
    });
  };

  /* ------------------------------------------------------------------ *
   * askRestart(featureCN) —— 文案是用户指定的，一个字都不能改
   * ------------------------------------------------------------------ */
  global.askRestart = function (featureCN) {
    return UI.showConfirm(
      (featureCN || '') + '需要重启生效，要重启吗？',
      '重启会关掉当前窗口再自己打开，正在跑的 Agent 也会一起重启。',
      {
        danger: false,
        okText: '等不及了，即刻重启！',
        cancelText: '先不要吧',
        risk: '重启期间正在执行的任务会中断，未发送的输入可能丢失。',
        icon: '↻',
        initialFocus: 'cancel'
      }
    );
  };

  /* ------------------------------------------------------------------ *
   * 原生 confirm 兜底：index.html 里还有 4 处 window.confirm(...)
   * 这些是同步调用，无法直接用 Promise，所以只在「浏览器环境」下保留原生行为，
   * 由后续的定点改写处理（见替换清单）。这里只做一个安全网：
   * 如果 UI 已经打开模态，拒绝再叠一层原生框。
   * ------------------------------------------------------------------ */
  var nativeConfirm = global.confirm;
  global.confirm = function (msg) {
    if (UI.isBlocking()) {
      try { console.warn('[ui-compat] 已有模态打开，忽略原生 confirm:', msg); } catch (e) {}
      return false;
    }
    return nativeConfirm.call(global, msg);
  };

  /* 给 CDP 自测用的标记 */
  global.__UI_COMPAT_READY = true;
  try {
    console.log('[ui-compat] 旧接口已转接到 window.UI v' + UI.version);
  } catch (e) {}

})(window);
