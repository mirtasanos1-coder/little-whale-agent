# -*- coding: utf-8 -*-
"""V2.0.2 自测：点歌事故四项修复（一次性脚本，跑完即删）。"""
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QQ_AGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="xy_v202_")

import agent
import sing

PASS = [0]
FAIL = []


def ok(name, cond, extra=""):
    if cond:
        PASS[0] += 1
        print("  [OK]   " + name)
    else:
        FAIL.append(name)
        print("  [FAIL] " + name + ("  <- " + str(extra) if extra else ""))


print("=" * 62)
print("V2.0.2 自测：点歌不再找不着词 / 不再乱发链接")
print("=" * 62)

# ---------- [1] 音乐库取词 ----------
print("\n[1] sing.preview：点《时过境迁》真的能拿到词")
lines, note = sing.lyrics_from_search("时过境迁 苏艺馨", max_lines=12)
ok("能拿到歌词（>=5 行）", len(lines) >= 5, "只有 %d 行 / %s" % (len(lines), note))
ok("note 里说明了来源", ("音乐库" in note) or ("取到" in note), note)
ok("歌词里没有时间轴残留（[00:12.34]）",
   not any("[" in ln and "]" in ln and ":" in ln for ln in lines),
   lines[:3])
ok("歌词里没有「作词/作曲/编曲」这类信息行",
   not any(k in ln for ln in lines for k in ("作词 :", "作曲 :", "编曲 :", "制作人", "未经")),
   lines[:5])
ok("preview() 和 lyrics_from_search() 给的是同一份",
   sing.preview("时过境迁 苏艺馨", max_lines=12)[0] == lines)

# ---------- [2] 歌名相关性（别拿同名的另一首歌凑数）----------
print("\n[2] 歌名匹配：对不上号就别硬认")
ok("完全一致得满分", sing._title_score("时过境迁", "时过境迁") == 1.0)
ok("带书名号/空格也认（归一化生效）",
   sing._title_score("《时过境迁》", "时过境迁 (Live)") >= 0.9)
ok("完全不同的歌名分数很低",
   sing._title_score("时过境迁", "乖乖待在我身边学英语") < 0.34,
   sing._title_score("时过境迁", "乖乖待在我身边学英语"))
bad_lines, bad_note = sing._lyrics_netease("zzz一个根本不存在的歌名zzz", max_lines=12)
ok("搜不到的歌老实返回空（不瞎编）", bad_lines == [], bad_lines[:3])

# ---------- [3] 搜索工具相关性过滤 ----------
print("\n[3] _filter_relevant：不相干的结果必须丢掉")
items = [
    {"title": "时过境迁 苏艺馨 歌词", "snippet": "时过境迁完整版歌词"},
    {"title": "乖乖待在我身边学英语不好吗", "snippet": "搞笑视频 播放量 1.2万"},
]
kept = agent._filter_relevant("时过境迁 歌词", items)
ok("相关的那条留下了", any("时过境迁" in (x.get("title") or "") for x in kept), kept)
ok("不相干的那条被丢掉", all("学英语" not in (x.get("title") or "") for x in kept), kept)
ok("全不相干时返回空列表（让模型自己圆场，而不是发假链接）",
   agent._filter_relevant("时过境迁", [items[1]]) == [])
ok("_rel_score 对完全相同给高分", agent._rel_score("时过境迁", "时过境迁 苏艺馨") > 0.9)
ok("_rel_score 对毫不相干给 0", agent._rel_score("时过境迁", "今天天气不错") == 0.0)

# ---------- [4] 先确认歌词，再清嗓子 ----------
print("\n[4] _sing_perform：没词就不清嗓子")
SENT = []
_real_send, _real_sing = agent.send_to_session, agent._sing_perform
agent.send_to_session = lambda sk, t, **k: (SENT.append(t), True)[1]

# 4a：歌词拿不到 —— 一条消息都不该发
_orig_preview = sing.preview
sing.preview = lambda name, max_lines=None: ([], "音乐库里有这首歌但没带歌词")
r1 = _real_sing("不存在的歌", "private_1", "private", "1", intro="咳咳，清唱一段给你~")
ok("没词时返回失败", r1.get("ok") is False, r1)
ok("没词时**没有**先发「咳咳，清唱一段」（这就是截图里的卡壳）", SENT == [], SENT)

# 4b：歌词拿得到 —— 先清嗓子，再把同一份词交给合成
SENT.clear()
CALLED = {}
_FAKE_WAV = os.path.join(os.environ["QQ_AGENT_DATA_DIR"], "fake.wav")
io.open(_FAKE_WAV, "wb").write(b"RIFF" + b"\x00" * 2048)

def _fake_sing(name, style="", lines=None, user_wants_song=True):
    CALLED["lines"] = lines
    return {"ok": True, "path": _FAKE_WAV, "engine": "read", "lines": lines or [], "note": ""}
_real_singmod = sing.sing
sing.sing = _fake_sing
sing.preview = lambda name, max_lines=None: (["第一句歌词", "第二句歌词", "第三句", "第四句", "第五句"],
                                             "从音乐库取到 5 行")
_orig_sv = agent.send_voice
agent.send_voice = lambda **k: True
r2 = _real_sing("时过境迁", "private_1", "private", "1", intro="咳咳，清唱一段给你~")
ok("有词时成功", r2.get("ok") is True, r2)
ok("有词时先把 intro 说出去", SENT and "清唱" in SENT[0], SENT)
ok("预检拿到的词直接交给合成（不重复搜一次）",
   CALLED.get("lines") == ["第一句歌词", "第二句歌词", "第三句", "第四句", "第五句"], CALLED.get("lines"))

sing.preview = _orig_preview
sing.sing = _real_singmod
agent.send_voice = _orig_sv
agent.send_to_session = _real_send

# ---------- [5] 主动分享不插进正在聊的天 ----------
print("\n[5] _share_video_target：正在聊就不打扰")
class _FakeMem:
    def __init__(self, age_sec):
        self.age = age_sec
    def session_stats(self):
        return [{"session_key": "private_3280735835", "last": time.time() - self.age}]
_real_mem = agent.mem
try:
    agent.mem = _FakeMem(5)                      # 5 秒前刚说过话
    ok("刚聊过（5 秒前）→ 不分享", agent._share_video_target() == "", agent._share_video_target())
    agent.mem = _FakeMem(3600)                   # 一小时前
    ok("安静一小时 → 允许分享（返回会话）",
       agent._share_video_target() == "private_3280735835", agent._share_video_target())
    agent.mem = _FakeMem(25 * 3600)              # 超过 24 小时
    ok("超过一天没聊 → 也不发", agent._share_video_target() == "")
finally:
    agent.mem = _real_mem

# ---------- [6] 禁止复读 ----------
print("\n[6] 同一个理由不许说第二遍")
import core.prompt_builder as _pb
ok("系统提示里有「同一个理由不许说第二遍」", "同一个理由不许说第二遍" in _pb.ANTI_BOT_RULES)
ok("仍然保留「像个人」和「看得见分享」两条原有规则",
   ("像个人，别像个程序" in _pb.ANTI_BOT_RULES) and ("看得见分享" in _pb.ANTI_BOT_RULES))
_real_perform = agent._sing_perform
agent._sing_perform = lambda *a, **k: {"ok": False, "msg": "没搜到这首歌的完整歌词，换一首试试吧"}
try:
    first = agent._run_chat_tool("sing_song", {"song": "x"}, scope="t:1", session_key="private_1")
    second = agent._run_chat_tool("sing_song", {"song": "x"}, scope="t:1", session_key="private_1")
    ok("第一次失败的提示里没有「别复读」", "别再用同一句话" not in first, first[:80])
    ok("第二次失败会提醒「别再用同一句话解释」", "别再用同一句话" in second, second[:120])
except Exception as e:
    ok("复读提醒可跑", False, "%s: %s" % (type(e).__name__, e))
finally:
    agent._sing_perform = _real_perform

print("\n" + "-" * 62)
print("通过 %d 项，失败 %d 项" % (PASS[0], len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("V2.0.2 自测全部通过")
