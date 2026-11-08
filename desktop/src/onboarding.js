/* ===========================================================================
   新手引导（Onboarding）—— V1.0.0-Beta 任务四
   ---------------------------------------------------------------------------
   三步：① 欢迎（这是啥、能干啥）
        ② 配模型（本地 LM Studio / Ollama，或者云端 API Key）
        ③ 完成（进控制台）
   判据：localStorage 的 xjy_onboarding_done —— 只认这个，跟「性能模式」那种
        要落到后端 config 的不一样（这里纯粹是界面引导，不需要后端知道）。
   顺序：免责协议 → 性能模式 → 环境向导 → 本引导 → 公告。
        别的模块等 window.__onboardingGate，不会抢在前面弹。
   =========================================================================== */
(function () {
  'use strict';

  var DONE_KEY = 'xjy_onboarding_done_v2';  // v1.4.1：换新标记，老用户也能再看一次新引导
  var STATE = { step: 1, chosen: 'local' };
  var _resolveGate;
  window.__onboardingGate = new Promise(function (r) { _resolveGate = r; });
  window.__onboarding = STATE;

  function done() {
    try { localStorage.setItem(DONE_KEY, '1'); } catch (e) { }
    var m = document.getElementById('obMask');
    if (m) m.remove();
    try { _resolveGate(); } catch (e) { }
  }
  function skip() {
    try { localStorage.setItem(DONE_KEY, '1'); } catch (e) { }
    var m = document.getElementById('obMask');
    if (m) m.remove();
    try { _resolveGate(); } catch (e) { }
    try { toast('随时可以在「设置 → 模型」里改，右上角帮助里也有说明', 'info'); } catch (e) { }
  }
  window.__onboardingDone = done;

  // v1.4.1：设置页「重新看一遍引导」按钮 —— 用 document 级委托，谁先加载都不影响
  document.addEventListener('click', function (ev) {
    var el = ev.target;
    while (el && el !== document) {
      if (el.id === 'btnOnbAgain') { ev.preventDefault(); window.__onboardingReopen(); return; }
      el = el.parentNode;
    }
  }, true);

  function css() {
    if (document.getElementById('obStyle')) return;
    var s = document.createElement('style');
    s.id = 'obStyle';
    s.textContent = [
      '#obMask{position:fixed;inset:0;z-index:180000;display:flex;align-items:center;justify-content:center;',
      'background:rgba(4,8,18,.72);backdrop-filter:blur(6px)}',
      '#obCard{width:min(680px,92vw);max-height:88vh;overflow:auto;border-radius:16px;padding:22px;',
      'background:linear-gradient(180deg,#131c2f,#0e1526);border:1px solid rgba(120,150,200,.22);',
      'box-shadow:0 24px 70px -24px rgba(0,0,0,.9);color:#e7edf6}',
      '.ob-h{font-size:17px;font-weight:650;letter-spacing:.2px}',
      '.ob-p{font-size:12.5px;line-height:1.85;color:#9fb0c8;margin-top:8px}',
      '.ob-dots{display:flex;gap:6px;align-items:center}',
      '.ob-dot{width:22px;height:3px;border-radius:9px;background:#26344c}',
      '.ob-dot.on{background:#38bdf8}',
      '.ob-row{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}',
      '.ob-in{flex:1;min-width:180px;padding:8px 10px;border-radius:9px;background:#0b1220;',
      'border:1px solid #24314a;color:#dbe6f5;font-size:12px;outline:none}',
      '.ob-in:focus{border-color:#38bdf8}',
      '.ob-btn{padding:8px 14px;border-radius:9px;font-size:12.5px;border:1px solid #24314a;background:#162033;color:#cfe0f5;cursor:pointer}',
      '.ob-btn:hover{background:#1d2a41}',
      '.ob-btn.pri{background:#0e7490;border-color:#0e7490;color:#fff}',
      '.ob-btn.pri:hover{background:#0b5f76}',
      '.ob-tip{font-size:11.5px;color:#7f92ab;margin-top:10px;line-height:1.7}',
      '.ob-choice{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}',
      '.ob-opt{flex:1;min-width:200px;padding:12px;border-radius:12px;cursor:pointer;',
      'background:#0f1728;border:1px solid #24314a}',
      '.ob-opt.on{border-color:#38bdf8;background:rgba(56,189,248,.08)}',
      '.ob-opt b{font-size:12.5px;color:#e7edf6}',
      '.ob-opt span{display:block;font-size:11px;color:#8a9cb5;margin-top:4px;line-height:1.6}',
      '.ob-res{font-size:11.5px;margin-top:10px;line-height:1.6;white-space:pre-wrap;color:#8a9cb5}',
      '.ob-foot{display:flex;align-items:center;gap:10px;margin-top:18px}',
    ].join('');
    document.head.appendChild(s);
  }

  function step1() {
    return '<div class="ob-h">先认识一下：小鲸鱼 🐳</div>'
      + '<div class="ob-p">它不是客服机器人，是一个会记事、有情绪的聊天对象。<br>'
      + '· <b>会记事</b>：你说过的事它记在本地数据库里（临时 / 长期 / 日记 / 好感度 / 学习）<br>'
      + '· <b>有情绪</b>：被夸会别扭，被凶会低落，不是「永远热情」<br>'
      + '· <b>能连 QQ</b>：内置 NapCat，扫码登录后就能在 QQ 里跟它聊<br>'
      + '· <b>数据都在你自己电脑上</b>：没有云端账号，删掉安装目录=全部消失</div>'
      + '<div class="ob-tip">点「下一步」开始，30 秒就能配好。</div>';
  }

  function step2() {
    var cloud = STATE.chosen === 'cloud';
    return '<div class="ob-h">第二步：给它一个「大脑」</div>'
      + '<div class="ob-p">没有模型它不会说话。二选一，之后都能在「设置 → 模型」里改。</div>'
      + '<div class="ob-choice">'
      + '  <div class="ob-opt ' + (cloud ? '' : 'on') + '" data-ob="local"><b>本地模型（免费、离线）</b>'
      + '    <span>LM Studio / Ollama 开一个服务就行，默认 http://127.0.0.1:1234/v1</span></div>'
      + '  <div class="ob-opt ' + (cloud ? 'on' : '') + '" data-ob="cloud"><b>云端 API（省电脑、要 Key）</b>'
      + '    <span>DeepSeek / 通义 / 任意 OpenAI 兼容服务，填 Key 即可</span></div>'
      + '</div>'
      + (cloud
        ? '<div class="ob-row"><input class="ob-in" id="obBase" placeholder="接口地址，如 https://api.deepseek.com/v1" value="https://api.deepseek.com/v1"></div>'
        + '<div class="ob-row"><input class="ob-in" id="obModel" placeholder="模型名，如 deepseek-chat" value="deepseek-chat"></div>'
        + '<div class="ob-row"><input class="ob-in" id="obKey" placeholder="API Key（sk-...）" type="password"></div>'
        : '<div class="ob-row"><input class="ob-in" id="obBase" placeholder="本机地址" value="http://127.0.0.1:1234/v1"></div>'
        + '<div class="ob-row"><input class="ob-in" id="obModel" placeholder="模型名（留空=自动用第一个）"></div>'
        + '<div class="ob-row"><button class="ob-btn" id="obList">看看有哪些模型</button>'
        + '<span class="ob-res" id="obListRes" style="margin-top:0"></span></div>')
      + '<div class="ob-row"><button class="ob-btn pri" id="obSave">保存并测试</button>'
      + '<span class="ob-res" id="obRes" style="margin-top:0"></span></div>'
      + '<div class="ob-tip">不确定选哪个？先用本地模型试试；本地服务没开的话，测试会明确告诉你连不上，不会影响别的功能。</div>';
  }

  function step3() {
    return '<div class="ob-h">好了，你可以开始了 ✅</div>'
      + '<div class="ob-p">进入控制台后，右上角有一个「开始」按钮 —— 点了它，小鲸鱼才真正开始收消息。<br>'
      + '第一次用建议按这个顺序：<br>'
      + '① 右上角「开始」→ Agent 起来<br>'
      + '② 左侧「聊天」里发一句试试（不用 QQ 也能测）<br>'
      + '③ 想在 QQ 里用：设置 → QQ 接入 → 扫码登录</div>'
      + '<div class="ob-tip">这些随时能在「设置」里改；本引导不会再来打扰你。</div>';
  }

  function render() {
    css();
    var m = document.getElementById('obMask');
    if (!m) {
      m = document.createElement('div');
      m.id = 'obMask';
      document.body.appendChild(m);
    }
    var dots = [1, 2, 3].map(function (i) {
      return '<i class="ob-dot ' + (STATE.step >= i ? 'on' : '') + '"></i>';
    }).join('');
    m.innerHTML = '<div id="obCard">'
      + '<div class="ob-dots">' + dots + '</div>'
      + '<div style="margin-top:14px">' + (STATE.step === 1 ? step1() : STATE.step === 2 ? step2() : step3()) + '</div>'
      + '<div class="ob-foot">'
      + (STATE.step > 1 ? '<button class="ob-btn" id="obBack">上一步</button>' : '')
      + '<div style="flex:1"></div>'
      + '<button class="ob-btn" id="obSkip">跳过</button>'
      + (STATE.step < 3
        ? '<button class="ob-btn pri" id="obNext">下一步</button>'
        : '<button class="ob-btn pri" id="obGo">进入控制台</button>')
      + '</div></div>';
    wire();
  }

  function wire() {
    var q = function (id) { return document.getElementById(id); };
    if (q('obBack')) q('obBack').onclick = function () { STATE.step--; render(); };
    if (q('obNext')) q('obNext').onclick = function () { STATE.step++; render(); };
    if (q('obSkip')) q('obSkip').onclick = skip;
    if (q('obGo')) q('obGo').onclick = function () {
      done();
      try { toast('小鲸鱼已就绪，点右上角「开始」', 'ok'); } catch (e) { }
    };
    Array.prototype.forEach.call(document.querySelectorAll('[data-ob]'), function (el) {
      el.onclick = function () { STATE.chosen = el.getAttribute('data-ob'); render(); };
    });
    if (q('obList')) q('obList').onclick = async function () {
      var res = q('obListRes');
      res.textContent = '正在问本机模型服务…';
      var base = (q('obBase') && q('obBase').value || '').trim();
      try {
        var r = await window.__bridge.call('set_model_base', base);
        var lm = await window.__bridge.call('list_local_models');
        var names = (lm && (lm.models || lm.items || lm.list)) || [];
        names = names.map(function (x) { return typeof x === 'string' ? x : (x.id || x.name || ''); }).filter(Boolean);
        res.textContent = names.length ? ('看到 ' + names.length + ' 个：' + names.slice(0, 6).join('、')) : '没看到模型（服务没开？先打开 LM Studio 并加载模型）';
        if (names.length && q('obModel') && !q('obModel').value) q('obModel').value = names[0];
      } catch (e) {
        res.textContent = '连不上：' + e;
      }
    };
    if (q('obSave')) q('obSave').onclick = async function () {
      var res = q('obRes');
      var btn = q('obSave');
      btn.disabled = true;
      res.textContent = '正在保存…';
      var base = (q('obBase') && q('obBase').value || '').trim();
      var model = (q('obModel') && q('obModel').value || '').trim();
      var key = (q('obKey') && q('obKey').value || '').trim();
      try {
        await window.__bridge.call('set_model_base', base);
        if (model) await window.__bridge.call('set_model', model);
        if (STATE.chosen === 'cloud' && key) {
          await window.__bridge.call('set_provider', 'custom');
          await window.__bridge.call('set_cloud_key', 'custom', key);
        }
        res.textContent = '已保存，正在测试…';
        var t = await window.__bridge.call('test_model');
        var ok = t && t.ok !== false;
        res.textContent = (ok ? '✅ 通了：' : '⚠ 没通：') + ((t && (t.msg || t.reply || t.model)) || '没有返回内容');
        if (ok) { STATE.step = 3; setTimeout(render, 700); }
      } catch (e) {
        res.textContent = '保存失败：' + e;
      }
      btn.disabled = false;
    };
  }

  // v1.4.1：给界面一个“重新看一遍”的入口（之前只有首启弹一次，弹完就再也找不回来）
  window.__onboardingReopen = function () {
    try { localStorage.removeItem(DONE_KEY); } catch (e) { }
    STATE.step = 1;
    render();
    try { toast('欢迎再看一遍，随时可以跳过', 'info'); } catch (e) { }
    return true;
  };

  async function show() {
    if (localStorage.getItem(DONE_KEY)) { try { _resolveGate(); } catch (e) { } return; }
    // 等免责协议 + 性能模式选完；环境向导如果存在也要等它关掉，别两个遮罩叠一起
    try { if (window.__firstRunGate) await window.__firstRunGate; } catch (e) { }
    if (window.__setupGate) {
      await Promise.race([window.__setupGate, new Promise(function (r) { setTimeout(r, 90 * 1000); })]);
    }
    if (localStorage.getItem(DONE_KEY)) { try { _resolveGate(); } catch (e) { } return; }
    var boot = document.getElementById('bootMask');
    if (boot && boot.style.display !== 'none') { await new Promise(function (r) { setTimeout(r, 1200); }); }
    render();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show);
  else show();
})();
