/* ==========================================================================
   小鲸鱼 · 首次启动引导编排（v1.3.0）
   --------------------------------------------------------------------------
   用户要的顺序是固定的，别乱：

     ① 免责协议（discMask，不接受就退出）—— 优先级最高
     ② 性能模式选择（本文件）—— 第二个弹窗
     ③ 环境初始化向导（setup-wizard.js）—— 缺什么装什么

   老版本的问题是 checkDisclaimer 和向导各自并发跑，向导经常抢在前面把
   免责协议盖住/挤到后面，用户还没看协议就先看到装 NapCat。

   实现方式：这里造一个 Promise 挂在 window.__firstRunGate 上，
   环境向导启动时 await 它 —— 也就是说「协议没同意、性能没选，向导不开」。
   ========================================================================== */
(function () {
  'use strict';

  var VER = 'v1.3.0';
  var STYLE_ID = 'frStyle';
  var MASK_ID = 'frMask';
  var DONE_KEY = 'perfModeAsked';

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // ---------------- 样式 ----------------
  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var css = ''
      + '#frMask{position:fixed;inset:0;z-index:100;display:none;align-items:center;justify-content:center;'
      + 'background:rgba(6,10,20,.72);backdrop-filter:blur(6px);padding:24px}'
      + '#frMask.on{display:flex}'
      + '#frMask *{box-sizing:border-box}'
      + '.fr-box{width:min(560px,92vw);background:linear-gradient(180deg,#16213a,#0d1526);'
      + 'border:1px solid rgba(120,160,255,.18);border-radius:16px;padding:26px 26px 20px;'
      + 'box-shadow:0 24px 64px rgba(0,0,0,.55);color:#e6ecff;font-size:14px;line-height:1.7}'
      + '.fr-box h3{margin:0 0 6px;font-size:17px;font-weight:600;letter-spacing:.2px}'
      + '.fr-sub{color:#8fa0c4;font-size:12.5px;margin-bottom:18px}'
      + '.fr-opt{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-radius:12px;'
      + 'border:1px solid rgba(120,160,255,.14);background:rgba(255,255,255,.02);cursor:pointer;'
      + 'transition:border-color .16s,background .16s;margin-bottom:10px}'
      + '.fr-opt:hover{border-color:rgba(120,160,255,.42);background:rgba(120,160,255,.07)}'
      + '.fr-opt.fr-sel{border-color:#4f8cff;background:rgba(79,140,255,.13)}'
      + '.fr-opt b{display:block;font-size:14px;margin-bottom:2px}'
      + '.fr-opt span{color:#8fa0c4;font-size:12px;line-height:1.6}'
      + '.fr-ico{font-size:20px;line-height:1.1}'
      + '.fr-foot{display:flex;align-items:center;justify-content:space-between;margin-top:16px;gap:12px}'
      + '.fr-tip{color:#7c8bab;font-size:11.5px}'
      + '.fr-btn{border:none;border-radius:10px;padding:9px 18px;font-size:13px;cursor:pointer;'
      + 'background:linear-gradient(180deg,#4f8cff,#2f6ae0);color:#fff;font-weight:600}'
      + '.fr-btn:hover{filter:brightness(1.08)}'
      + '.fr-btn.fr-ghost{background:transparent;border:1px solid rgba(120,160,255,.3);'
      + 'color:#c8d6f5;font-weight:500}';
    var st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  }

  // ---------------- 等免责协议 ----------------
  // 后端没起来时 window.__DISCLAIMER_STATE 一直是 'pending'，
  // 等太久（30 秒）就不再拦着，免得用户对着黑屏以为卡死。
  // v1.3.0：只要协议还没「显示出来」，就一直等（最长 5 分钟）——
  // 协议没弹出来之前，后面的性能模式弹窗绝不能抢在前面。
  async function waitDisclaimer() {
    for (var i = 0; i < 1200; i++) {
      var s = window.__DISCLAIMER_STATE;
      if (s === 'done') return true;
      if (s === 'shown') {
        // 正等用户勾选，继续等（最多再等 10 分钟，之后放行）
        for (var j = 0; j < 3000; j++) {
          if (window.__DISCLAIMER_STATE === 'done') return true;
          await sleep(200);
        }
        return true;
      }
      await sleep(250);
    }
    return true;
  }

  // ---------------- 性能模式弹窗 ----------------
  function buildModal() {
    injectStyle();
    var old = document.getElementById(MASK_ID);
    if (old) old.remove();
    var mask = document.createElement('div');
    mask.id = MASK_ID;
    mask.innerHTML =
      '<div class="fr-box" role="dialog" aria-modal="true" aria-label="选择运行模式">'
      + '  <h3>先选一个运行模式</h3>'
      + '  <div class="fr-sub">你的电脑如果比较老（核显、4G 内存、风扇一直响），选「性能模式」会舒服很多。</div>'
      + '  <div class="fr-opt fr-sel" data-mode="normal">'
      + '    <div class="fr-ico">✨</div>'
      + '    <div><b>正常模式（推荐）</b>'
      + '      <span>保留全部动效：毛玻璃、切页动画、水波纹、背景飘雪。看着舒服，但会吃一点显卡。</span></div>'
      + '  </div>'
      + '  <div class="fr-opt" data-mode="low">'
      + '    <div class="fr-ico">🍃</div>'
      + '    <div><b>性能模式（低配电脑）</b>'
      + '      <span>动效、毛玻璃、粒子全停，界面变成干脆利落的方框。功能一个不少，只是不"滑"了。</span></div>'
      + '  </div>'
      + '  <div class="fr-foot">'
      + '    <div class="fr-tip">后面可以在「设置 → 通用」里随时改</div>'
      + '    <button class="fr-btn" id="frOk">就用这个</button>'
      + '  </div>'
      + '</div>';
    document.body.appendChild(mask);

    var picked = 'normal';
    mask.querySelectorAll('.fr-opt').forEach(function (el) {
      el.addEventListener('click', function () {
        picked = el.getAttribute('data-mode') || 'normal';
        mask.querySelectorAll('.fr-opt').forEach(function (x) { x.classList.remove('fr-sel'); });
        el.classList.add('fr-sel');
      });
    });

    return new Promise(function (resolve) {
      mask.querySelector('#frOk').addEventListener('click', function () { resolve(picked); });
      mask.classList.add('on');
    }).then(function (mode) {
      mask.classList.remove('on');
      setTimeout(function () { try { mask.remove(); } catch (e) {} }, 200);
      return mode;
    });
  }

  function toastSafe(msg, type) {
    try { if (typeof window.toast === 'function') return window.toast(msg, type); } catch (e) {}
    console.log('[first-run]', msg);
  }

  async function savePerf(mode) {
    var low = mode === 'low';
    try { if (window.__applyPerfMode) window.__applyPerfMode(low); } catch (e) {}
    try {
      localStorage.setItem(DONE_KEY, String(Date.now()));
    } catch (e) {}
    if (window.__bridge && window.__bridge.call) {
      try {
        await window.__bridge.call('save_settings', [{ perf_mode: low, perf_mode_asked: true }]);
      } catch (e) { console.error('[first-run] save_settings', e); }
    }
    return low;
  }

  async function askPerfMode() {
    // 以「配置里问过没」为准（配置跟着数据目录走，换机器/换目录都会重新问）。
    // localStorage 只当后端没起来时的兜底 —— 它是跟 WebView2 用户目录走的，
    // 拿它当主判据会导致：换了新数据目录、界面却死活不再弹（实测踩过）。
    var st = null;
    if (window.__bridge && window.__bridge.call) {
      try { st = await window.__bridge.call('get_settings', []); } catch (e) { st = null; }
    }
    var asked = false;
    if (st && st.ok !== false) asked = !!st.perf_mode_asked;
    else { try { asked = !!localStorage.getItem(DONE_KEY); } catch (e) {} }
    // 已经问过：直接按配置应用，不再打扰
    if (asked) {
      try { if (window.__applyPerfMode) window.__applyPerfMode(!!(st && st.perf_mode)); } catch (e) {}
      return;
    }
    var mode = await buildModal();
    await savePerf(mode);
    toastSafe(mode === 'low' ? '已切到性能模式，动效全停' : '用的正常模式，动效都开着', 'ok');
  }

  // ---------------- 编排 ----------------
  function domReady() {
    if (document.readyState !== 'loading') return Promise.resolve();
    return new Promise(function (r) { document.addEventListener('DOMContentLoaded', function () { r(); }, { once: true }); });
  }

  var gate = (async function run() {
    // 本文件在 <head> 里就被加载了，这会儿 document.body 还是 null，
    // appendChild 会直接抛错（第一版实测就是弹不出来）。等 DOM 就绪再画。
    await domReady();
    try { await waitDisclaimer(); } catch (e) { console.error('[first-run] disclaimer', e); }
    try { await askPerfMode(); } catch (e) { console.error('[first-run] perf', e); }
  })();

  window.__firstRunGate = gate;
  window.__firstRun = {
    version: VER,
    gate: gate,
    askPerfMode: askPerfMode,
    state: function () { return { disclaimer: window.__DISCLAIMER_STATE, perf_low: !!window.__PERF_LOW }; }
  };
})();
