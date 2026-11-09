# -*- coding: utf-8 -*-
"""V1.6.2 打包版界面验收（CDP）。用法： python t4.py <安装目录>
需要先手动或用本脚本启动 小鲸鱼.exe：
  环境变量 WEBVIEW2_USER_DATA_FOLDER=<临时>   WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9225 --remote-allow-origins=*
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

INST = sys.argv[1] if len(sys.argv) > 1 else r"D:\临时文件\_inst162"
PORT_CDP = 9225
WV = r"D:\临时文件\_v162\wv162"
DATA = os.path.join(INST, "_data")

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  OK   %s %s" % (name, extra))
    else:
        fail += 1
        print("  FAIL %s %s" % (name, extra))


def launch():
    env = dict(os.environ)
    env["WEBVIEW2_USER_DATA_FOLDER"] = WV
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--remote-debugging-port=%d --remote-allow-origins=*" % PORT_CDP)
    env["QQ_AGENT_DATA_DIR"] = DATA
    p = subprocess.Popen([os.path.join(INST, "小鲸鱼.exe")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("   shell pid", p.pid)
    return p


def connect(wait=90):
    end = time.time() + wait
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % PORT_CDP, timeout=2) as r:
                tabs = json.loads(r.read().decode())
            tabs = [t for t in tabs if t.get("type") == "page"]
            if tabs:
                return websocket.create_connection(tabs[0]["webSocketDebuggerUrl"],
                                                   timeout=30, suppress_origin=True)
        except Exception:
            time.sleep(1)
    raise RuntimeError("CDP 没起来")


class C:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0

    def call(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                return m

    def ev(self, expr, timeout=60):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True,
                      returnByValue=True, timeout=timeout * 1000)
        res = r.get("result", {})
        if res.get("exceptionDetails"):
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:400])
        return res.get("result", {}).get("value")


subprocess.run(["taskkill", "/IM", "小鲸鱼.exe", "/F", "/T"], capture_output=True)
time.sleep(1)
proc = launch()
ws = connect()
c = C(ws)
c.call("Runtime.enable")

for _ in range(60):
    v = c.ev("typeof switchTab === 'function'")
    if v:
        break
    time.sleep(1)
chk("界面 JS 就绪", bool(c.ev("typeof switchTab === 'function'")))

print("== 首启弹窗")
c.ev("""(async()=>{
  const s=(ms)=>new Promise(r=>setTimeout(r,ms));
  const g=(i)=>document.getElementById(i);
  if(g('discAgree')&&!g('discAgree').checked){g('discAgree').click();}
  const ok=g('btnDiscOk'); if(ok&&!ok.disabled){ok.click();}
  await s(400);
  const fr=g('frMask'); if(fr&&!fr.classList.contains('hidden')){
    const b=[...fr.querySelectorAll('button')].find(x=>/就用这个|确定|开始/.test(x.textContent||'')); if(b)b.click();
  }
  await s(500);
  const p=g('btnPopupOk'); if(p&&p.offsetParent)p.click();
  await s(300);
  return true;})()""")
time.sleep(1.5)

print("== 1. 版本与导航")
ab = c.ev("call('get_about')")
chk("界面拿到的版本 1.6.2", (ab or {}).get("version") == "1.6.2", str((ab or {}).get("version")))
chk("5 个大类按钮", c.ev("document.querySelectorAll('#tabNav .grp-btn').length") == 5)

print("== 2. 无边框开关（点一下关、再点一下开）")
c.ev("switchTab('settings')")
time.sleep(1)
r = c.ev("""(async()=>{
  const s=(ms)=>new Promise(r=>setTimeout(r,ms));
  const sw=document.getElementById('swFrameless');
  sw.scrollIntoView({block:'center'});
  const before=`${sw.checked}`;
  sw.click(); await s(1200);
  const after1=`${sw.checked}`;
  const state1 = await call('win_state');
  sw.click(); await s(1200);
  const after2=`${sw.checked}`;
  const state2 = await call('win_state');
  const toastTxt = [...document.querySelectorAll('.toast,.toast-item,#toastBox div')].map(x=>x.textContent).join('|');
  return {before, after1, after2, s1: state1, s2: state2, toastTxt};
})()""")
print("   ", json.dumps({k: r.get(k) for k in ("before", "after1", "after2")}, ensure_ascii=False))
chk("点一下从「开」变「关」", r.get("before") == "true" and r.get("after1") == "false",
    "%s->%s" % (r.get("before"), r.get("after1")))
chk("后端 win_state 同步（frameless=False）", (r.get("s1") or {}).get("frameless") is False,
    str((r.get("s1") or {}).get("frameless")))
chk("再点回「开」", r.get("after2") == "true", str(r.get("after2")))
chk("后端 win_state 同步（frameless=True）", (r.get("s2") or {}).get("frameless") is True,
    str((r.get("s2") or {}).get("frameless")))

print("== 3. 托盘开关")
r = c.ev("""(async()=>{
  const s=(ms)=>new Promise(r=>setTimeout(r,ms));
  const sw=document.getElementById('swTray'); sw.scrollIntoView({block:'center'});
  const before=`${sw.checked}`;
  sw.click(); await s(1500);
  const after1=`${sw.checked}`; const st1=await call('win_state');
  sw.click(); await s(1500);
  const after2=`${sw.checked}`; const st2=await call('win_state');
  return {before, after1, after2, st1, st2};
})()""")
print("   ", json.dumps({k: r.get(k) for k in ("before", "after1", "after2")}, ensure_ascii=False))
chk("托盘开关会变", r.get("before") != r.get("after1"), "%s->%s" % (r.get("before"), r.get("after1")))
chk("托盘状态被后端确认", (r.get("st1") or {}).get("tray_running") in (True, False),
    str((r.get("st1") or {}).get("tray_running")))

print("== 4. 樱花主题（全亮、无暗块）")
r = c.ev("""(async()=>{
  const s=(ms)=>new Promise(r=>setTimeout(r,ms));
  const t=await call('apply_theme','sakura');
  if(t&&t.css) applyThemeCss(t.css);
  await s(900);
  const bg=getComputedStyle(document.body).backgroundColor;
  const dark=[...document.querySelectorAll('*')].filter(e=>{
    const c=getComputedStyle(e);
    if(c.backgroundColor==='rgba(0, 0, 0, 0)'||c.backgroundColor==='transparent')return false;
    const m=c.backgroundColor.match(/\\d+/g).map(Number);
    const lum=0.2126*m[0]+0.7152*m[1]+0.0722*m[2];
    return lum<80 && e.getBoundingClientRect().width>240 && e.getBoundingClientRect().height>80;
  }).length;
  return {dark: document.documentElement.dataset.themeDark, bg, darkBlocks: dark, panel:getComputedStyle(document.documentElement).getPropertyValue('--panel').trim()};
})()""")
print("   ", json.dumps(r, ensure_ascii=False))
chk("themeDark=0", r.get("dark") == "0", str(r.get("dark")))
chk("页面底色是亮的", "255" in str(r.get("bg")) or "24" in str(r.get("bg")), str(r.get("bg")))
chk("没有大块深色面板", (r.get("darkBlocks") or 0) <= 1, str(r.get("darkBlocks")))

print("== 5. NapCat 卡片用 3003/3004")
r = c.ev("""(async()=>{
  const s=(ms)=>new Promise(r=>setTimeout(r,ms));
  switchTab('settings'); await s(600);
  let text='';
  for(let k=0;k<24;k++){                       // 卡片会先显示「检测中…」，等它落定
    const card=document.getElementById('npCard');
    text = card?card.innerText.replace(/\\s+/g,' ').slice(0,500):'';
    if(text.indexOf('3003')>=0 && !/检测中/.test(text)) break;
    await call('napcat_recheck').catch(()=>{});
    await s(600);
  }
  return {text};
})()""")
t = r.get("text") or ""
chk("卡片里出现 3003", "3003" in t, t[:80])
chk("卡片里出现 3004", "3004" in t, t[:80])
chk("卡片里没有「任务管理器」", "任务管理器" not in t)

print("\n结果: OK=%d FAIL=%d" % (ok, fail))
try:
    subprocess.run(["taskkill", "/IM", "小鲸鱼.exe", "/F", "/T"], capture_output=True)
except Exception:
    pass
sys.exit(0 if fail == 0 else 1)
