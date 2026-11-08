"""V1.3.2 回归自检（QA 可跑：python tests\\test_v132_qc.py）。

覆盖这一版最容易被改坏的几处：
  1. 版本号单一来源（version.py 与 config/backend/tauri 一致）
  2. 配置热更新（改完不用重启，下一句就生效 + ast 漂移检查）
  3. 好感度「当天不误扣」
  4. BUG-08「记住:」不外泄、内容进永久记忆
  5. BUG-10 备份导出目录与列表同源
  6. 第四阶段：av_module 里有没有 av_server.exe（有就不用装 Python）
不依赖网络、不启动界面，几秒钟跑完。
"""
import ast
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="xzj_qc_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

FAILED = []
PASSED = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    (PASSED if ok else FAILED).append(name)
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  — " + str(detail)) if detail else ""))
    return ok


# ---------------------------------------------------------------- 1. 版本号
def t_version_single_source():
    import version
    import config
    assert version.APP_VERSION == config.APP_VERSION and version.APP_VERSION.count(".") == 2, \
        (version.APP_VERSION, config.APP_VERSION)
    src = open(os.path.join(ROOT, "backend_server.py"), encoding="utf-8").read()
    assert "from version import" in src, "backend_server 没从 version.py 取版本"
    tauri = json.load(open(os.path.join(ROOT, "desktop", "src-tauri", "tauri.conf.json"),
                           encoding="utf-8"))
    assert tauri["version"] == version.APP_VERSION, tauri["version"]
    return True, "%s（version/config/backend/tauri 一致）" % version.APP_VERSION


# ------------------------------------------------------------ 2. 配置热更新
def t_live_config():
    import agent
    import config
    cfg = config.load()
    old = cfg.get("temperature")
    cfg["temperature"] = 1.23
    config.save(cfg)
    agent._CFG_MTIME = -1  # 模拟「界面刚保存」
    live = agent._live_cfg("temperature", 0.75)
    ok1 = abs(float(live) - 1.23) < 1e-6
    # 漂移检查：import 期由 _CFG 派生的顶层名字，必须在 _reload_constants 里有刷新
    tree = ast.parse(open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read())
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            seg = ast.get_source_segment(open(os.path.join(ROOT, "agent.py"),
                                              encoding="utf-8").read(), node.value) or ""
            if "_CFG" in seg or "_fnum(" in seg:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
    src = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
    body = src[src.find("def _reload_constants"):]
    body = body[:body.find("\ndef ", 10)]
    missing = sorted(n for n in names if ('g["%s"]' % n) not in body)
    cfg["temperature"] = old
    config.save(cfg)
    assert ok1, "改配置后 _live_cfg 没跟上"
    assert not missing, "这些常量只在 import 时算一次：%s" % missing
    return True, "热更新 OK；%d 个派生常量全部在刷新清单里" % len(names)


# ------------------------------------------------------- 3. 好感度当天不扣
def t_affinity_today():
    import memory
    m = memory.Memory()
    m.bump_affinity("u_qc", delta=20)
    c = m.conn.cursor()
    c.execute("SELECT score, last_decay_day FROM affinity WHERE user_id=?", ("u_qc",))
    row = c.fetchone()
    assert row, "没建出好感度记录"
    assert row[1] == time.strftime("%Y-%m-%d"), "首次写入没记 last_decay_day（%r）" % (row[1],)
    before = int(row[0] or 0)
    m.decay_affinity()
    c.execute("SELECT score FROM affinity WHERE user_id=?", ("u_qc",))
    after = int((c.fetchone() or [0])[0] or 0)
    assert before == after, "当天就被扣分了：%s -> %s" % (before, after)
    return True, "首次写入记今天，当天不扣分（%s）" % before


# --------------------------------------------------------- 4. 「记住:」剥离
def t_remember_strip():
    import agent
    raw = "好呀～\n- 记住: （阿凯）养了一只叫团子的橘猫\n记住：他讨厌香菜\n今天有点累"
    agent._remember_begin()
    clean = agent.clean_reply(raw)
    items = agent._remember_take()
    assert "记住" not in clean, "正文里还留着「记住」：%r" % clean
    assert "团子的橘猫" in "".join(items) and "讨厌香菜" in "".join(items), items
    assert "好呀" in clean and "有点累" in clean, "正文被过度删除"
    return True, "剥离 %d 条：%s" % (len(items), " / ".join(items))


# ------------------------------------------------------- 5. 备份目录同源
def t_backup_dir():
    import mem_transfer
    d = mem_transfer.export_dir()
    assert os.path.isdir(d), d
    os.environ["QQ_AGENT_DATA_DIR"] = TMP
    assert os.path.abspath(d).startswith(os.path.abspath(TMP)), \
        "导出目录跑到数据目录外面去了：%s" % d
    return True, d


# ------------------------------------------------- 6. av_server.exe 是否随包
def t_av_exe():
    exe = os.path.join(ROOT, "resources", "av_module", "av_server.exe")
    assert os.path.isfile(exe), "没有 av_server.exe（用户机器没 Python 就用不了）"
    mb = os.path.getsize(exe) / 1048576
    assert mb > 100, "av_server.exe 只有 %.1fMB，像是打漏了依赖" % mb
    bridge = open(os.path.join(ROOT, "av_bridge.py"), encoding="utf-8").read()
    assert "av_server.exe" in bridge, "av_bridge 不认 exe"
    build = open(os.path.join(ROOT, "build_backend.py"), encoding="utf-8").read()
    assert "不重复塞进后端 exe" in build, "build_backend 可能又会把 145MB 塞两份"
    return True, "%.1fMB，av_bridge/build_backend 都已配套" % mb


# --------------------------------- 7. provider_keys 必须加密落盘（QA 终审必修 3）
def t_provider_keys_encrypted():
    import config as cfg_mod
    import app_ui
    r = app_ui.save_settings({"provider_keys": {"deepseek": "sk-qc-plain-key", "openai": ""}})
    assert r.get("ok"), r
    cfg = cfg_mod.load()
    pk = cfg.get("provider_keys") or {}
    val = str(pk.get("deepseek") or "")
    assert val.startswith("dpapi:"), "provider_keys 还是明文落盘：%r" % val[:20]
    assert "sk-qc-plain" not in json.dumps(cfg, ensure_ascii=False), "配置文件里还有明文 Key"
    # get_config 也不能把敏感项（含 dpapi: 密文）吐给前端
    gc = app_ui.get_config()
    blob = json.dumps(gc.get("config") or {}, ensure_ascii=False)
    low = blob.lower()
    assert "provider_keys" not in low, "get_config 把 provider_keys 吐出去了"
    assert "dpapi:" not in low, "get_config 把 dpapi: 密文吐出去了"
    assert "max_tokens" in low, "过滤过火：max_tokens 这种正常配置也被滤掉了"
    return True, "deepseek 是 dpapi: 密文；get_config 已滤敏感项"


import version as _v
print("小鲸鱼 V%s 回归自检（数据目录 %s）" % (_v.APP_VERSION, TMP))
check("版本号单一来源", t_version_single_source)
check("配置热更新 + 漂移检查", t_live_config)
check("好感度当天不扣分", t_affinity_today)
check("「记住:」不外泄", t_remember_strip)
check("备份目录与列表同源", t_backup_dir)
check("音视频 exe 随包", t_av_exe)
check("provider_keys 加密落盘 + get_config 不吐密文", t_provider_keys_encrypted)

print("")
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
sys.exit(1 if FAILED else 0)
