# -*- coding: utf-8 -*-
"""V1.9.18 自测：QQ 开着也能把 NapCat 拉起来。

用户实机报的是「natcap一直拉不起来」。根因不在 NapCat，在我们自己的 launch()：
只要看见 QQ.exe 就 return False，让人先去退 QQ —— 而「退 QQ」正是用户说过的下策。
这里锁住四件事，免得以后又被改回去：

1. 本机 QQ 开着时 launch() 不许直接拒绝，要照常启动；
2. 快速登录撞到「已登录,无法重复登录」要记成 conflict、并且自动换下一个号；
3. 目录里留着的旧二维码不能被当成新码（否则明明登上了还说「在等扫码」）；
4. 登录弹窗要把 qq_open / conflict 说清楚，并把「退出我的 QQ 并接管」变成一个按钮。

全程打桩，不真启动 NapCat、不碰真 QQ。

跑法：python tests\\test_v1918_login.py
"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="xy_v1918_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_ui  # noqa: E402
import napcat_mgr  # noqa: E402

PASS, FAIL = [], []

REAL = {}   # 打桩前的真函数，最后要还原（同一进程里别的测试还要用）


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s %s" % (name, extra))


class FakeNapCat(object):
    """把 launch() 依赖的外部世界全部拦住：进程表、端口、日志、二维码。"""

    def __init__(self):
        self.spawned = []
        self.spawned_argv = []
        self.conflicts = {}
        self.bads = {}
        self.oks = []
        self.log_errors = {}
        self.port_up = False
        self.qr = ""
        self.qq_pids = [{"pid": 4242, "exe": r"D:\fake\QQ.exe", "name": "QQ.exe"}]
        self.cands = ["1111111111", "2222222222"]
        self.log_path = os.path.join(TMP, "napcat_launch.log")
        with open(self.log_path, "ab") as f:
            f.write(b"\n")

    # ---- 装桩 ----
    def patch(self):
        m = napcat_mgr
        for name in ("find_install_dir", "find_launcher", "_napcat_script", "is_shell_build",
                     "_bundled_node", "_shell_env", "running_pids", "napcat_procs",
                     "port_open", "qq_client_procs", "qq_running", "account_uins",
                     "login_candidates", "conflict_uins", "mark_uin_conflict", "mark_uin_bad",
                     "mark_uin_ok", "launch_log_errors", "launch_log_path", "launch_log_tail",
                     "qr_path", "stop_all", "_spawn", "port_owner", "stale_pids"):
            REAL[name] = getattr(m, name, None)
        m.find_install_dir = lambda *a, **k: r"D:\fake\napcat"
        m.find_launcher = lambda *a, **k: "napcat.bat"
        m._napcat_script = lambda *a, **k: "index.js"
        m.is_shell_build = lambda *a, **k: True
        m._bundled_node = lambda *a, **k: "node.exe"
        m._shell_env = lambda *a, **k: {}
        m.running_pids = lambda *a, **k: []
        m.napcat_procs = lambda *a, **k: []
        m.port_open = lambda *a, **k: self.port_up
        m.qq_client_procs = lambda *a, **k: list(self.qq_pids)
        m.qq_running = lambda *a, **k: bool(self.qq_pids)
        m.account_uins = lambda *a, **k: list(self.cands)
        m.login_candidates = lambda *a, **k: list(self.cands)
        m.conflict_uins = lambda *a, **k: dict(self.conflicts)
        m.mark_uin_conflict = lambda u, why="": self.conflicts.setdefault(str(u), time.time())
        m.mark_uin_bad = lambda u, why="": self.bads.setdefault(str(u), time.time())
        m.mark_uin_ok = lambda u, why="": self.oks.append(str(u))
        m.launch_log_errors = lambda *a, **k: dict(self.log_errors)
        m.launch_log_path = lambda *a, **k: self.log_path
        m.launch_log_tail = lambda *a, **k: ""
        m.qr_path = lambda *a, **k: self.qr
        m.stop_all = lambda *a, **k: (0, "stub")
        m.port_owner = lambda *a, **k: {}
        m.stale_pids = lambda *a, **k: []
        m._spawn = self._fake_spawn

    def _fake_spawn(self, args, **kw):
        self.spawned.append(list(args))
        self.spawned_argv.append(list(args))
        return None

    def restore(self):
        for name, fn in REAL.items():
            if fn is not None:
                setattr(napcat_mgr, name, fn)
        REAL.clear()


def make_qr_file(age_sec):
    """造一张 age_sec 秒之前写出来的二维码文件。"""
    p = os.path.join(TMP, "qrcode.png")
    with open(p, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    ts = time.time() - float(age_sec)
    os.utime(p, (ts, ts))
    return p


# ============================================================ 1. QQ 开着也要启动
def t_no_refuse():
    print("\n[1] 官方 QQ 开着时，launch() 不许再拒绝启动")
    fake = FakeNapCat()
    fake.patch()
    try:
        ok_, msg = napcat_mgr.launch(r"D:\fake\napcat", wait=0)
        ok("QQ 开着也返回 ok=True", ok_ is True, "msg=%r" % msg)
        ok("不再出现「我不会去关你的 QQ」", "我不会去关你的 QQ" not in msg, msg)
        ok("不再要求用户先去退 QQ", "请在官方 QQ 里退出登录" not in msg, msg)
        ok("确实起了进程", len(fake.spawned) == 1, str(fake.spawned))
        argv = fake.spawned[0] if fake.spawned else []
        ok("仍然优先用缓存快速登录", "-q" in argv and "1111111111" in argv, str(argv))
        # 日志里要留一条「照常启动」，出问题时好对账
        with open(fake.log_path, "rb") as f:
            log = f.read().decode("utf-8", "ignore")
        ok("启动日志记下「本机 QQ 客户端开着」", "本机 QQ 客户端开着" in log, log[-200:])
    finally:
        fake.restore()


# ============================================================ 2. 被占的号要自动跳过
def t_skip_occupied():
    print("\n[2] 被官方 QQ 占着的号：记 conflict，并自动换下一个号")
    fake = FakeNapCat()
    fake.patch()
    try:
        # 第一个号被占：先让 launch 把 1111111111 用掉，然后报 conflict
        fake.log_errors = {"code": "qq_conflict", "uin": "1111111111"}
        ok_, msg = napcat_mgr.launch(r"D:\fake\napcat", wait=2)
        ok("1111111111 被记成 conflict", "1111111111" in fake.conflicts, str(fake.conflicts))
        tried = [a for a in fake.spawned if "-q" in a]
        second = [a for a in tried if "2222222222" in a]
        ok("自动换了第二个号去试", bool(second), str(tried))
        ok("没有把号记成「登录态失效」", "1111111111" not in fake.bads, str(fake.bads))
        ok("失败时如实说原因是 QQ 开着", "你的官方 QQ 正开着" in msg or "都被它占着" in msg,
           "msg=%r" % msg)

        # 已经有 conflict 记录时，一开始就别再拿它去撞
        fake2 = FakeNapCat()
        fake2.patch()
        try:
            fake2.conflicts = {"1111111111": time.time()}
            napcat_mgr.launch(r"D:\fake\napcat", wait=0)
            argv = fake2.spawned[0] if fake2.spawned else []
            ok("已知被占的号不再当第一选择", "1111111111" not in argv, str(argv))
            ok("直接用另一个号", "2222222222" in argv, str(argv))
        finally:
            fake2.restore()
    finally:
        fake.restore()


# ============================================================ 3. 旧二维码不算数
def t_stale_qr():
    print("\n[3] 目录里留着的旧二维码，不能被当成「正在等扫码」")
    fake = FakeNapCat()
    fake.patch()
    try:
        fake.qr = make_qr_file(3600)      # 一小时前的码
        ok_, msg = napcat_mgr.launch(r"D:\fake\napcat", wait=2)
        ok("旧码不被当成新码", "等扫码登录" not in msg, "msg=%r" % msg)
        ok("旧码路径不写进提示", fake.qr not in msg, "msg=%r" % msg)

        fake.qr = make_qr_file(0)         # 这次启动写出来的码
        ok2, msg2 = napcat_mgr.launch(r"D:\fake\napcat", wait=2)
        ok("新码才算「在等扫码」", "等扫码登录" in msg2, "msg=%r" % msg2)
        ok("新码路径给出去", fake.qr in msg2, "msg=%r" % msg2)
    finally:
        fake.restore()


# ============================================================ 4. 弹窗把话说清楚
def t_panel():
    print("\n[4] 登录弹窗：qq_open / conflict / 提示 / 接管按钮")
    saved = {}
    keys = ("probe", "login_info", "find_install_dir", "qq_client_procs", "conflict_uins",
            "uin_state", "login_candidates", "qr_png", "launch_log_errors")
    for k in keys:
        saved[k] = getattr(napcat_mgr, k, None)
    saved["webui_mod"] = app_ui._webui_mod
    try:
        napcat_mgr.probe = lambda deep=False: {"state": "not_running", "running": False,
                                               "proc_count": 0, "dir": r"D:\fake\napcat",
                                               "msg": ""}
        napcat_mgr.login_info = lambda timeout=2: {}
        napcat_mgr.find_install_dir = lambda *a, **k: r"D:\fake\napcat"
        napcat_mgr.qq_client_procs = lambda *a, **k: [{"pid": 4242, "exe": "QQ.exe"}]
        napcat_mgr.conflict_uins = lambda *a, **k: {"2145216414": time.time()}
        napcat_mgr.uin_state = lambda *a, **k: {"ok": {"2145216414": time.time(),
                                                      "3280735835": time.time()},
                                               "bad": {},
                                               "last": "2145216414"}
        napcat_mgr.login_candidates = lambda *a, **k: ["2145216414", "3280735835"]
        napcat_mgr.qr_png = lambda *a, **k: {}     # 没码，走「还没启动」分支
        app_ui._webui_mod = lambda: None           # WebUI 不可用 → 降级路径
        app_ui._LOGIN_PANEL_CACHE.update(ts=0.0, data=None)

        p = app_ui.qq_login_panel()
        ok("panel 报出 qq_open", p.get("qq_open") is True, str(p.get("qq_open")))
        ok("panel 报出 can_takeover", p.get("can_takeover") is True, str(p.get("can_takeover")))
        ok("panel 报出被占的号", p.get("conflict") == ["2145216414"], str(p.get("conflict")))
        ok("有一句提示解释为什么登不上", bool(p.get("hint")), str(p.get("hint")))
        ok("提示里给出两条路（退出接管 / 换号）",
           ("接管" in (p.get("hint") or "")) and ("另一个" in (p.get("hint") or "")),
           str(p.get("hint")))
        ok("状态不是「现在没法登录」", p.get("mode") != "busy", str(p.get("mode")))
        labels = [a.get("label") for a in p.get("accounts") or []]
        ok("每个号都有说明文字（不再只剩号码）", all(labels), str(labels))
        occ = [a for a in p.get("accounts") or [] if a.get("occupied")]
        ok("被占的号标成 occupied 并有说明",
           bool(occ) and all("你的 QQ 正用着" in (a.get("label") or "") for a in occ), str(occ))
        last = [a for a in p.get("accounts") or [] if a.get("uin") == "2145216414"]
        ok("被占的号带上「你的 QQ 正用着」", bool(last) and "你的 QQ 正用着" in (last[0].get("label") or ""),
           str(labels))
        first = (p.get("accounts") or [{}])[0]
        ok("被占的号排到后面，能用的一号在前", first.get("uin") == "3280735835", str(labels))
        notes = " ".join(p.get("notes") or [])
        ok("notes 里点名被占的号", "2145216414" in notes, notes)
    finally:
        for k, v in saved.items():
            if k == "webui_mod":
                app_ui._webui_mod = v
            elif v is not None:
                setattr(napcat_mgr, k, v)
        app_ui._LOGIN_PANEL_CACHE.update(ts=0.0, data=None)


# ============================================================ 5. 接管按钮真的会退 QQ
def t_takeover():
    print("\n[5] 「退出我的 QQ 并接管」：只有点了才会退 QQ")
    saved = {"launch": napcat_mgr.launch,
             "qq_client_procs": napcat_mgr.qq_client_procs,
             "find_install_dir": napcat_mgr.find_install_dir}
    seen = {}
    real_panel = app_ui.qq_login_panel
    try:
        def fake_launch(install_dir="", wait=0, allow_second_qq=False, restart=False,
                        close_qq=False, takeover=False):
            seen.update(wait=wait, restart=restart, close_qq=close_qq, takeover=takeover)
            return True, "已连上：QQ 1111111111"

        napcat_mgr.launch = fake_launch
        napcat_mgr.qq_client_procs = lambda *a, **k: [{"pid": 4242, "exe": "QQ.exe"}]
        napcat_mgr.find_install_dir = lambda *a, **k: r"D:\fake\napcat"
        app_ui.qq_login_panel = lambda **k: {"ok": True, "mode": "logged_in"}

        r = app_ui.qq_login_takeover()
        ok("接管返回 ok", r.get("ok") is True, str(r))
        ok("接管时 close_qq=True（真的去退 QQ）", seen.get("close_qq") is True, str(seen))
        ok("接管时 restart=True（清掉卡住的旧实例）", seen.get("restart") is True, str(seen))
        ok("接管返回面板数据", isinstance(r.get("panel"), dict), str(type(r.get("panel"))))

        seen.clear()
        nag = app_ui.qq_login_panel
        # 普通「启动并出码」绝不能退 QQ
        app_ui.qq_login_panel = lambda **k: {"ok": True}
        try:
            import config as _c
            _c.get = lambda k, d=None: (True if k == "napcat_keep_qq" else d)
            app_ui.start_napcat(wait=0)
            ok("普通启动不退 QQ", seen.get("close_qq") in (False, None), str(seen))
        finally:
            app_ui.qq_login_panel = nag
    finally:
        for k, v in saved.items():
            setattr(napcat_mgr, k, v)
        app_ui.qq_login_panel = real_panel


def main():
    print("=" * 62)
    print("V1.9.18 QQ 登录补丁自测（全程打桩，不真启动 NapCat）")
    print("=" * 62)
    t_no_refuse()
    t_skip_occupied()
    t_stale_qr()
    t_panel()
    t_takeover()
    print("\n" + "-" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：" + f)
        return 1
    print("V1.9.18 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
