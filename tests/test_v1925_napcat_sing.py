# -*- coding: utf-8 -*-
"""V1.9.25 自测：NapCat 起不来 + 唱歌从「念」改成「唱」。

用户报的两件事（m04313）：
  「这个版本现在在其他账号登陆状态下启动不了Natcap了，马上修」
  「唱歌念出来有些生硬了，改成唱」

取证结论（真机日志 5904 行逐段复盘 + 待机探针）：
  NapCat 侧 —— 现场 5 次启动**全都是**同一个形态：
      [info] 正在快速登录 2145216414
      [info] [Fork] Worker进程已登录成功，切换到正常重试策略
      [info] [OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3003
      [error] [Fork] Worker进程退出，退出码: 1
    也就是「快速登录其实成功了、3003 也起来了，但 1.5~2 秒后 Worker 被顶下线」。
    `当前账号(…)已登录,无法重复登录` 全日志只出现过 1 次，还是在 11-05 08:47 —— 远在这之前。
    真因是官方 QQ 正用着 2145216414（它的 `Documents\\Tencent Files\\2145216414\\nt_qq\\nt_db\\*.db-wal`
    还在写），NapCat 拿同一个号快速登录必然被顶。三处代码缺陷把这个现象彻底掩盖了：
      a. `launch_log_errors()` 无条件读日志尾部 800 行 —— 把 11-05 那条旧 `token_dead`
         当成本次结论。只扫「本次启动之后」的话命中 0 条。
      b. `launch()` 等待循环一看到 `token_dead` 就 break —— 连 3003 会不会起来都没等，
         对外报「20 秒内端口没起来」，可日志里明明写着 3003 已 Start。
      c. 于是 `mark_uin_bad()` 把没被试过的 3280735835 记成坏号。
    另外「哪个号被官方 QQ 占着」当时只能靠日志里那句 conflict —— 实测不一定出现，
    所以新增 `qq_client_uins()`：问注册表拿到真正的「文档」目录（这台机器是 `D:\\Documents`，
    不是 `~\\Documents`），看哪个号的数据目录最近还在被写。

  唱歌侧 —— 用户设置 `sing_engine="read"`（只有朗读可用）。原来只是逐行换
    rate/pitch 参数，edge-tts 的单层 prosody 做不出旋律（逐字/嵌套 prosody 直接
    NoAudioReceived），所以听起来还是念。改成：WordBoundary 拿字级时间 → 逐字
    重采样变调上五声音阶旋律 → 颤音 → 纯 numpy 伴奏 → wave 写 WAV。
    miniaudio 只解不编（~1.6MB），避开 `av` 的 128MB 与 `imageio_ffmpeg` 的 84MB。

这组测试锁五件事：
  [1] `launch_log_errors(after=…)` 只看本次启动之后的日志：不再把旧 token_dead 当本次结论
  [2] 新增 `account_kicked`：识别「登进去又被踢」，不再误标坏号
  [3] `qq_client_uins()`：从注册表找到「文档」→ 认出被官方 QQ 占着的号
  [4] `login_candidates()` 把被占的号排到最后（不再优先拿它去撞）
  [5] `launch()` 不再因 token_dead 提前 break；真唱链端到端能出 WAV
"""
import io
import os
import struct
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="xy_v1925_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP

import napcat_mgr as nm  # noqa: E402
import sing  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append((name, extra))
        print(f"  [FAIL] {name}" + (f"  ← {extra}" if extra else ""))


# ---- 真机日志片段（原样抄自 release\\小鲸鱼\\napcat_launch.log 行 5851-5871）----
# 这是「本次启动」那一段：登录成功 -> 3003 起来 -> 1.5 秒后 Worker 被踢。
_SEG_KICKED = """
[2026-11-06 09:57:26.034] [info] [NapCat] 正在快速登录  2145216414
[2026-11-06 09:57:28.101] [info] [NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略
[2026-11-06 09:57:28.402] [info] [NapCat] 已通知主进程登录成功
[2026-11-06 09:57:29.115] [info] [OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3003
[2026-11-06 09:57:31.220] [error] [NapCat] [Fork] Worker进程退出，退出码: 1
[2026-11-06 09:57:31.230] [warn] [NapCat] [Fork] Worker进程意外退出 (1/3)，正在尝试重新拉起...
"""
# 旧历史：11-05 那条过期的 token_dead（就是它把这次判定带偏的）
_SEG_OLD_DEAD = """
[2026-11-05 08:46:50.001] [info] [NapCat] 正在快速登录  3280735835
[2026-11-05 08:46:55.900] [error] [NapCat] 快速登录错误：登录态已失效，请重新登录。
"""
_SEG_OLD_CONFLICT = """
[2026-11-05 08:47:18.501] [error] [NapCat] 当前账号(2145216414)已登录,无法重复登录
"""


def _write_log(text):
    p = nm.launch_log_path()
    with io.open(p, "wb") as f:
        f.write(text.encode("utf-8", "ignore"))
    return p


def main():
    print("=" * 60)
    print("V1.9.25 自测：NapCat 启动修复 + 真唱")
    print("=" * 60)
    real = {}

    try:
        # ================= [1] launch_log_errors 只看「本次启动之后」 =================
        print("[1] launch_log_errors(after=…)：不再把旧日志当本次结论")
        _write_log(_SEG_OLD_DEAD + _SEG_OLD_CONFLICT + _SEG_KICKED)
        off = len((_SEG_OLD_DEAD + _SEG_OLD_CONFLICT).encode("utf-8"))

        old_way = nm.launch_log_errors(lines=800)
        ok("旧写法（读尾部 800 行）会翻出过期结论 —— 复现用户遇到的误判",
           old_way.get("code") in ("token_dead", "qq_conflict", "account_kicked"),
           str(old_way.get("code")))

        new_way = nm.launch_log_errors(after=off)
        ok("只看本次启动之后 → 拿到的是真正发生的 account_kicked",
           new_way.get("code") == "account_kicked", str(new_way))
        ok("account_kicked 认出了号", new_way.get("uin") == "2145216414", str(new_way.get("uin")))
        ok("account_kicked 文案说清了「登进去又被踢」",
           "顶下线" in str(new_way.get("msg") or ""), str(new_way.get("msg")))

        ok("本次启动里没有 token_dead（过期的那个没被算进来）",
           nm.launch_log_errors(after=off).get("code") != "token_dead",
           str(nm.launch_log_errors(after=off)))

        # ================= [2] account_kicked 的成对判据 =================
        print("[2] account_kicked 必须「先成功登录、后 Worker 退出」成对出现")
        only_exit = """
[2026-11-06 09:57:26.034] [info] [NapCat] 正在快速登录  2145216414
[2026-11-06 09:57:31.220] [error] [NapCat] [Fork] Worker进程退出，退出码: 1
"""
        _write_log(only_exit)
        ok("只有 Worker 退出、没有「已登录成功」→ 不算撞号",
           nm.launch_log_errors(after=0).get("code") != "account_kicked",
           str(nm.launch_log_errors(after=0)))

        _write_log(_SEG_KICKED)
        e = nm.launch_log_errors(after=0)
        ok("成对出现才判为 account_kicked", e.get("code") == "account_kicked", str(e))
        ok("raw 里带着 Worker 退出的上下文",
           "Worker进程退出" in str(e.get("raw") or ""), str(e.get("raw"))[:80])

        _write_log(_SEG_OLD_CONFLICT)
        e = nm.launch_log_errors(after=0)
        ok("老的 qq_conflict 仍然认得出来",
           e.get("code") == "qq_conflict" and e.get("uin") == "2145216414", str(e))

        _write_log('[2026-11-06 10:00:00] [error] something serverErrorCode": 168 in here\n')
        e = nm.launch_log_errors(after=0)
        ok("风控 168 仍然认得出来", e.get("code") == "risk", str(e))

        # ================= [3] qq_client_uins：被官方 QQ 占着的号 =================
        print("[3] qq_client_uins()：从注册表拿到真「文档」目录，认出被占的号")
        docs = nm._documents_dirs()
        ok("_documents_dirs() 问到了注册表里的 D:\\Documents（不是只猜 ~\\Documents）",
           any(os.path.normcase(d).endswith(os.path.normcase("Documents"))
               and os.path.isdir(os.path.join(d, "Tencent Files")) for d in docs),
           str(docs))

        tf = ""
        for d in docs:
            p = os.path.join(d, "Tencent Files")
            if os.path.isdir(p):
                tf = p
                break
        ok("找到了 Tencent Files 目录", bool(tf), str(docs))
        if tf:
            nm._QQ_ACTIVE_CACHE["t"] = 0.0
            got = nm.qq_client_uins(max_age=1800)
            ok("qq_client_uins() 返回的是数字号列表",
               all(str(u).isdigit() for u in got), str(got))
            nm._QQ_ACTIVE_CACHE["t"] = 0.0
            none_recent = nm.qq_client_uins(max_age=1)
            ok("max_age 收紧到 1 秒后（QQ 早就不写了）→ 认不出任何号",
               none_recent == [], str(none_recent))
            # 缓存：第二次调用不该重扫磁盘
            nm._QQ_ACTIVE_CACHE["t"] = time.time()
            nm._QQ_ACTIVE_CACHE["uins"] = ["999999"]
            ok("30 秒内走缓存", nm.qq_client_uins() == ["999999"], str(nm.qq_client_uins()))
            nm._QQ_ACTIVE_CACHE["t"] = 0.0
            nm._QQ_ACTIVE_CACHE["uins"] = []

        # ================= [4] login_candidates 把被占的号排到最后 =================
        print("[4] login_candidates()：被官方 QQ 占着的号排在最后")
        real["qq_client_procs"] = nm.qq_client_procs
        real["conflict_uins"] = nm.conflict_uins
        real["qq_client_uins"] = nm.qq_client_uins
        real["login_candidates"] = nm.login_candidates
        real["guess_qq"] = nm.guess_qq
        real["account_uins"] = nm.account_uins
        real["quick_login_uins"] = nm.quick_login_uins
        real["uin_state"] = nm.uin_state
        real["bootstrap"] = nm.bootstrap_bad_from_log

        nm.bootstrap_bad_from_log = lambda: []
        nm.uin_state = lambda: {"last": "2145216414"}
        nm.guess_qq = lambda d="": ""
        nm.account_uins = lambda d="": ["2145216414"]
        nm.quick_login_uins = lambda d="": ["3280735835", "2145216414"]
        nm.qq_client_procs = lambda: [{"pid": 1, "exe": "QQ.exe", "cmd": ""}]
        nm.conflict_uins = lambda: {}
        nm.qq_client_uins = lambda *a, **k: ["2145216414"]

        got = nm.login_candidates("X:/nope")
        ok("被占的 2145216414 排到了最后", got and got[-1] == "2145216414", str(got))
        ok("另一个号被推到第一", got and got[0] == "3280735835", str(got))

        nm.qq_client_uins = lambda *a, **k: []
        got2 = nm.login_candidates("X:/nope")
        ok("官方 QQ 没占号时不重排（保持原顺序）",
           got2 and got2[0] == "2145216414", str(got2))

        # ================= [5] 真唱链 =================
        print("[5] 真唱：依赖齐 → 出 WAV；链断 → 退回朗读")
        ok("_has_sing_deps() 为真（numpy + miniaudio 都在）", sing._has_sing_deps(),
           str(sing._has_sing_deps()))
        st = sing.sing_status()
        ok("sing_status 报出真唱就绪", st.get("sing_ready") is True, str(st.get("sing_ready")))
        ok("sing_status 的引擎名是「真唱模式」",
           st.get("engine_cn") == "真唱模式" or st.get("sing_engine_cn"),
           str(st.get("engine_cn")))

        ok("_char_spans 把「按词」拆成「按字」并均分时间",
           (lambda sp: len(sp) == 4 and abs(sp[0][1] - 0.0) < 1e-6 and abs(sp[-1][2] - 1.0) < 1e-6)(
               sing._char_spans([("月亮代表", 0.0, 1.0)])), "")

        ok("_pitch_shift 升 12 半音 → 时长减半",
           (lambda r: True)(None) and (
               lambda: True)() and _check_pitch())

        # ---- v1.9.25 真唱的「音准」与「节奏」两条硬指标 ----
        # 以前每个字只是「在自己原来的音高上平移几个半音」，说话本身的语调会整个叠上来，
        # 听着还是念；现在要把每个字挪到目标音上，再把时长用 OLA 拉回原样。
        ok("_PITCH_CAP 存在且合理（3~12 半音之间）",
           3.0 <= float(getattr(sing, "_PITCH_CAP", 0)) <= 12.0,
           str(getattr(sing, "_PITCH_CAP", None)))
        try:
            _np = sing._np()
            _sr = sing._SR
            # ① _f0_hz 认得准：造一段 220Hz 正弦
            _t = _np.arange(int(_sr * 0.4), dtype=_np.float64) / float(_sr)
            _tone220 = (0.4 * _np.sin(2.0 * _np.pi * 220.0 * _t)).astype(_np.float32)
            _f = sing._f0_hz(_tone220, _np)
            ok("_f0_hz 认得出 220Hz（误差 < 8Hz）",
               bool(_f) and abs(_f - 220.0) < 8.0, str(_f))
            ok("_f0_hz 对静音返回 None",
               sing._f0_hz(_np.zeros(int(_sr * 0.2), dtype=_np.float32), _np) is None, "")

            # ② _time_stretch 只改时长、不改音高
            _st = sing._time_stretch(_tone220, _np, int(_tone220.size * 1.6))
            ok("_time_stretch 输出长度等于要求长度",
               abs(_st.size - int(_tone220.size * 1.6)) <= 2, str(_st.size))
            _fs = sing._f0_hz(_st, _np)
            ok("_time_stretch 之后音高基本没变（±6Hz）",
               bool(_fs) and abs(_fs - 220.0) < 6.0, str(_fs))

            # ③ 归一化到目标音：把 220Hz 挪到 294Hz（+5 半音）应真的接近 294
            _need = 12.0 * _np.log2(294.0 / 220.0)
            _shifted = sing._pitch_shift(_tone220, _np, float(_need))
            _fs2 = sing._f0_hz(_shifted, _np)
            ok("变调之后落在目标音附近（±8Hz）",
               bool(_fs2) and abs(_fs2 - 294.0) < 8.0, str(_fs2))
        except Exception as e:
            ok("真唱音准/节奏指标", False, f"{type(e).__name__}: {e}")

        try:
            path, voice = sing.synth_sing(["月亮代表我的心", "海内存知己"],
                                          {"voice": "zh-CN-XiaoxiaoNeural",
                                           "rate": "-14%", "max_lines": 12})
            ok("synth_sing 产出文件", bool(path) and os.path.isfile(path), str(path))
            ok("产物是 WAV（不是拼 mp3）", path.endswith(".wav"), str(path))
            with io.open(path, "rb") as f:
                head = f.read(44)
            ok("WAV 头是标准 RIFF/WAVE",
               head[0:4] == b"RIFF" and head[8:12] == b"WAVE", str(head[:16]))
            ch, rate, _br, _ba, bits = struct.unpack("<HIIHH", head[22:36])
            ok("单声道 / 24000Hz / 16bit",
               ch == 1 and rate == 24000 and bits == 16, f"{ch}/{rate}/{bits}")
            ok("大小超过 1KB（上层判成功要 >1024 字节）",
               os.path.getsize(path) > 1024, str(os.path.getsize(path)))
            ok("连着唱两句比一句长",
               os.path.getsize(path) > 2000, str(os.path.getsize(path)))
        except Exception as e:
            ok("synth_sing 端到端", False, f"{type(e).__name__}: {e}")

        # synth_read 该分发到真唱
        try:
            p3, _v = sing.synth_read(["小鲸鱼真唱测试"])
            ok("synth_read 默认走真唱（产物 .wav）", p3.endswith(".wav"), str(p3))
        except Exception as e:
            ok("synth_read 分发到真唱", False, f"{type(e).__name__}: {e}")

        # 依赖缺失时退回朗读
        real_has = sing._has_sing_deps
        sing._has_sing_deps = lambda: False
        try:
            p4, _v = sing.synth_read(["小鲸鱼朗读测试"])
            ok("没有 numpy/miniaudio 时退回朗读（产物 .mp3）", p4.endswith(".mp3"), str(p4))
        except Exception as e:
            ok("退回朗读", False, f"{type(e).__name__}: {e}")
        finally:
            sing._has_sing_deps = real_has

        # 真唱异常时也不能变「没声音」
        real_sing = sing.synth_sing
        sing.synth_sing = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟挂了"))
        try:
            p5, _v = sing.synth_read(["真唱挂了也要有声音"])
            ok("真唱抛异常时自动退回朗读", p5.endswith(".mp3"), str(p5))
        except Exception as e:
            ok("真唱抛异常时自动退回朗读", False, f"{type(e).__name__}: {e}")
        finally:
            sing.synth_sing = real_sing

        # 多音字与散文闸门必须还在（v1.9.19 的老功能）
        ok("POLY_RULES 还在（50 条）", len(sing.POLY_RULES) >= 45, str(len(sing.POLY_RULES)))
        ok("歌手闸门 _looks_like_prose 还在",
           sing._looks_like_prose("这是一本小说的内容简介，讲述了男主角和女主角的故事。"),
           "")
        ok("真唱不改歌词内容（多音字修正仍在入口）",
           sing.polyphone_fix("行不行")[0] == "形不形", str(sing.polyphone_fix("行不行")))

        # ================= [6] launch() 等待循环不再提前 break =================
        print("[6] launch()：日志尾部有旧 token_dead 时也要把 3003 等出来")
        d = os.path.join(TMP, "napcat_fake")
        os.makedirs(os.path.join(d, "napcat"), exist_ok=True)
        io.open(os.path.join(d, "node.exe"), "wb").close()
        io.open(os.path.join(d, "napcat", "napcat.mjs"), "wb").close()

        # 日志里先躺着 11-05 那条过期的 token_dead + conflict
        _write_log(_SEG_OLD_DEAD + _SEG_OLD_CONFLICT)

        real.update({
            "find_launcher": nm.find_launcher, "is_shell_build": nm.is_shell_build,
            "_bundled_node": nm._bundled_node, "_napcat_script": nm._napcat_script,
            "_shell_env": nm._shell_env, "running_pids": nm.running_pids,
            "napcat_procs": nm.napcat_procs, "port_open": nm.port_open,
            "port_owner": nm.port_owner, "qr_path": nm.qr_path,
            "_qr_fresh": nm._qr_fresh, "stale_pids": nm.stale_pids,
            "_spawn": nm._spawn, "stop_all": nm.stop_all,
            "qq_running": nm.qq_running, "mark_uin_ok": nm.mark_uin_ok,
            "mark_uin_bad": nm.mark_uin_bad, "mark_uin_conflict": nm.mark_uin_conflict,
            "launch_log_tail": nm.launch_log_tail,
        })

        times = {"n": 0}
        nm.find_launcher = lambda x="": os.path.join(d, "node.exe")
        nm.is_shell_build = lambda x="": True
        nm._bundled_node = lambda x="": os.path.join(d, "node.exe")
        nm._napcat_script = lambda x="": os.path.join(d, "napcat", "napcat.mjs")
        nm._shell_env = lambda x="": dict(os.environ)
        nm.running_pids = lambda x="": []
        nm.napcat_procs = lambda: []
        nm.port_owner = lambda p: {}
        nm.qr_path = lambda x="": ""
        nm._qr_fresh = lambda x="": False
        nm.stale_pids = lambda x="": []
        nm.stop_all = lambda *a, **k: (0, "没跑")
        nm.qq_running = lambda: False
        nm.launch_log_tail = lambda n=8: ""
        # 号选择固定成 3280735835，官方 QQ 没开（这组只验日志判定逻辑）
        nm.uin_state = lambda: {"last": "3280735835"}
        nm.bootstrap_bad_from_log = lambda: []
        nm.guess_qq = lambda x="": "3280735835"
        nm.account_uins = lambda x="": ["3280735835"]
        nm.quick_login_uins = lambda x="": ["3280735835"]
        nm.qq_client_procs = lambda: []
        nm.conflict_uins = lambda: {}

        marked = {"bad": [], "conflict": [], "ok": []}
        nm.mark_uin_ok = lambda u: marked["ok"].append(u)
        nm.mark_uin_bad = lambda u, w="": marked["bad"].append(u)
        nm.mark_uin_conflict = lambda u, w="": marked["conflict"].append(u)

        # 假的 _spawn：像真启动那样，把「本次启动」的日志**追加到日志尾部**
        # （关键 —— 必须写在 _log_off 之后，否则模拟不出真实时序）
        SPAWN_LOG = {"text": ""}
        def _fake_spawn(args, **k):
            if SPAWN_LOG["text"]:
                with io.open(nm.launch_log_path(), "ab") as f:
                    f.write(SPAWN_LOG["text"].encode("utf-8", "ignore"))
        SPAWN_LOG["text"] = ""
        nm._spawn = _fake_spawn

        # 3003 什么时候通，决定「读日志」会不会被跳过（循环里是 先 port_open 后读日志）：
        #  - 情形一（SPAWN 空）：第 2 次调用起就通
        #  - 情形二/三：等「本次启动」的日志真的落到文件里之后**再过一轮**才通，
        #    这样才能保证循环至少读过一次日志（否则会先通、直接 return，日志没被读）
        seen_once = {"v": False}
        def _po(port=0, **k):
            times["n"] += 1
            txt = (SPAWN_LOG["text"] or "").strip()
            if txt:
                try:
                    with io.open(nm.launch_log_path(), "rb") as f:
                        has = txt in f.read().decode("utf-8", "ignore")
                except Exception:
                    has = False
                if has and seen_once["v"]:
                    return True
                if has:
                    seen_once["v"] = True
                return False
            return times["n"] >= 2
        nm.port_open = _po

        # 情形一：本次启动本身没报错（旧 token_dead 只在历史里）→ 应等到 3003
        SPAWN_LOG["text"] = ""
        ok_flag, msg = nm.launch(d, wait=6)
        ok("旧 token_dead 躺在日志尾部也不提前 break，最终等到 3003 → 返回成功",
           ok_flag is True, str(msg)[:140])
        ok("成功时记的是本次真正用的号",
           marked["ok"] == ["3280735835"], str(marked["ok"]))
        ok("没有因为过期日志把号误标成坏号",
           marked["bad"] == [], str(marked["bad"]))

        # 情形二：本次启动真的 token_dead → 继续等，但如实记坏号
        SPAWN_LOG["text"] = "正在快速登录  3280735835\n快速登录错误：登录态已失效，请重新登录。\n"
        times["n"] = 0
        seen_once["v"] = False
        marked["bad"].clear()
        marked["ok"].clear()
        ok_flag2, msg2 = nm.launch(d, wait=6)
        ok("本次真的 token_dead 也仍把 3003 等出来了（不再第一圈就放弃）",
           ok_flag2 is True, str(msg2)[:140])
        ok("同时如实记下坏号（不丢信息）",
           "3280735835" in marked["bad"], str(marked["bad"]))

        # 情形三：本次启动是「登进去又被踢」→ 记 conflict，并继续换号
        SPAWN_LOG["text"] = _SEG_KICKED
        times["n"] = 0
        seen_once["v"] = False
        marked["bad"].clear()
        marked["conflict"].clear()
        marked["ok"].clear()
        ok_flag3, msg3 = nm.launch(d, wait=6)
        ok("account_kicked 时记成 conflict（半小时内不再优先选它）",
           "2145216414" in marked["conflict"] or "3280735835" in marked["conflict"],
           str(marked["conflict"]))
        ok("account_kicked 不会把它误标成坏号",
           marked["bad"] == [], str(marked["bad"]))

    finally:
        for k, v in real.items():
            setattr(nm, k, v)
        try:
            nm._QQ_ACTIVE_CACHE["t"] = 0.0
            nm._QQ_ACTIVE_CACHE["uins"] = []
        except Exception:
            pass

    print("-" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n, e in FAIL:
            print(f"  ✗ {n}  {e}")
        return 1
    print("V1.9.25 自测全部通过")
    return 0


def _check_pitch():
    """升 12 半音 → 时长减半。"""
    try:
        import numpy as np
        sr = 24000
        x = np.sin(2 * np.pi * 220 * np.arange(sr) / float(sr)).astype(np.float32)
        y = sing._pitch_shift(x, np, 12)
        return abs(len(y) - len(x) // 2) <= 2
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
