"""发消息这一层 —— 私聊/群聊/语音/图片/表情包（从 agent.py 物理搬出来，逻辑没改）。

拆分原则（见 core/__init__.py）：只搬运，不改业务逻辑；agent.py 顶部把这些名字重新
import 回去，所以 `agent.send_private_human(...)` 依然有效。

跨模块的「活」名字（拆分前它们就是 agent 的全局）：
  · `napcat`  —— 这里是转发函数，每次用都去取 agent.napcat 当前那一个，
                 所以不会被导入瞬间的旧快照卡住。
  · `_live_cfg` —— 转发 agent._live_cfg，配置改了立刻生效，不是导入时的快照。
  · `STICKER_ENABLED` / `STICKER_DIR_CFG` / `MEMORY_ENABLED` —— 这些会被
    agent._reload_constants() 就地刷新，所以函数里现取（_g.live），不吃快照。
  · `mem` / `sessions` —— 同上，现取。
"""
import asyncio
import os
import random
import re
import time

import config as cfg_mod
import stickers as stickers_mod
from core import globals as _g
from core.utils import log, clean_reply  # noqa: F401


# ---------------------------------------------------------------
# 与 agent 同源的名字（拆出去以后仍然只有一份真值）
# ---------------------------------------------------------------
DATA_DIR = cfg_mod.DATA_DIR
"""数据目录。和 agent.DATA_DIR 都来自 config._data_dir()，逐行相同。"""

HUMANIZE_SPLIT_MIN = 40
"""拟人分条的最短长度兜底（真值走配置 humanize_split_min，这里只是取不到配置时的默认）。"""


def _live_cfg(key, default=None):
    """读实时配置 —— 等价于 agent._live_cfg，配置改了立刻生效。"""
    return _g.cfg(key, default)


def napcat(action, **params):
    """转发到 agent.napcat(action, **params) —— 那是 NapCat 的 HTTP 调用助手。

    为什么不写 `from agent import napcat`：那只是导入瞬间的快照，而且 agent 里这个
    助手依赖 NAPCAT_HTTP（配置改了会重算），所以每次现取。
    """
    return _g.call("napcat", action, **params)


# ------------------------------------------------------------------
# 搬自 agent.py L3323-3323：_PAIR_RE
# ------------------------------------------------------------------
_PAIR_RE = re.compile(r"《[^》]*》|「[^」]*」|『[^』]*』|【[^】]*】|（[^）]*）|\([^)]*\)|“[^”]*”")


# ------------------------------------------------------------------
# 搬自 agent.py L3324-3324：_PAIR_OPEN
# ------------------------------------------------------------------
_PAIR_OPEN = "\ue000"


# ------------------------------------------------------------------
# 搬自 agent.py L3325-3325：_PAIR_CLOSE
# ------------------------------------------------------------------
_PAIR_CLOSE = "\ue001"


# ------------------------------------------------------------------
# 搬自 agent.py L3328-3340：_protect_pairs
# ------------------------------------------------------------------
def _protect_pairs(text):
    """把《…》《…！》这类成对符号整段换成占位符，拆句时就不会从里面断开。

    QA P2-1：分享文案里标题带「！」，以前会被切开，用户看到的两条气泡
    第二条从「》」开始。占位符用私有区字符，正常文本里不会出现。
    """
    saved = []

    def _sub(m):
        saved.append(m.group(0))
        return "%s%d%s" % (_PAIR_OPEN, len(saved) - 1, _PAIR_CLOSE)

    return _PAIR_RE.sub(_sub, str(text or "")), saved


# ------------------------------------------------------------------
# 搬自 agent.py L3343-3349：_restore_pairs
# ------------------------------------------------------------------
def _restore_pairs(text, saved):
    s = str(text or "")
    if not saved:
        return s
    for i, v in enumerate(saved):
        s = s.replace("%s%d%s" % (_PAIR_OPEN, i, _PAIR_CLOSE), v)
    return s


# ------------------------------------------------------------------
# 搬自 agent.py L3352-3352：_PAIR_TAIL
# ------------------------------------------------------------------
_PAIR_TAIL = "》」』”’】）)]}"


# ------------------------------------------------------------------
# 搬自 agent.py L3355-3404：split_reply
# ------------------------------------------------------------------
def split_reply(text):
    """把一段回复拆成多条发（拟人）。

    QA 终审 P2-1：以前 `HUMANIZE_SPLIT`/`HUMANIZE_SPLIT_MIN` 写死在模块顶部、
    段数**没有上限** —— 模型回 4 行就真发 4 条消息，段间还随机 sleep 1~3 秒。
    现在全部实时读配置，并且最多拆 `humanize_split_max`（默认 3）段，超出合并进最后一段。
    """
    text = str(text or "")
    try:
        if not _live_cfg("humanize_split", True):
            return [text]
        _min = max(10, int(_live_cfg("humanize_split_min", 40) or 40))
        _max = max(1, min(10, int(_live_cfg("humanize_split_max", 3) or 3)))
    except Exception:
        _min, _max = HUMANIZE_SPLIT_MIN, 3
    if len(text) < _min:
        return [text]
    chunks = [c.strip() for c in text.split("\n") if c.strip()]
    if len(chunks) < 2:
        # v1.9.11.2 补丁（QA P2-1）：书名号/引号里的「！」不算断句点。
        # 以前《…！…》会被从「！」切开，第二条气泡从「》」开始，很难看。
        _prot, _saved = _protect_pairs(text)
        sentences = [_restore_pairs(s, _saved)
                     for s in re.split(r"(?<=[。！？!?~])", _prot)]
        chunks, buf = [], ""
        for s in sentences:
            buf += s
            if len(buf) >= _min:
                chunks.append(buf.strip())
                buf = ""
        if buf.strip():
            chunks.append(buf.strip())
        # 兜底：万一还是切在了收尾符号前，把那段并回上一条
        _fixed = []
        for _c in chunks:
            if _fixed and _c and _c[0] in _PAIR_TAIL:
                _fixed[-1] += _c
            else:
                _fixed.append(_c)
        chunks = _fixed
    merged = []
    for c in chunks:
        if merged and len(merged[-1]) + len(c) < _min // 2:
            merged[-1] += "\n" + c
        else:
            merged.append(c)
    if len(merged) > _max:
        # 超过上限就合并：最后一段吞掉剩下的，宁可长一点也不刷屏
        merged = merged[:_max - 1] + ["\n".join(merged[_max - 1:])]
    return merged if merged else [text]


# ------------------------------------------------------------------
# 搬自 agent.py L3418-3427：_reply_segment
# ------------------------------------------------------------------
def _reply_segment(msg_id):
    """构造引用回复段。msg_id 为空时返回空列表（方便直接 extend）。

    NapCat 要求 reply 段必须是 message 数组的第一项，否则引用不生效。
    实测：send_private_msg(message=[{"type":"reply","data":{"id":"123"}}, ...])
          → {"status":"ok","retcode":0,"data":{"message_id":732405459}}
    """
    if not msg_id:
        return []
    return [{"type": "reply", "data": {"id": str(msg_id)}}]


# ------------------------------------------------------------------
# 搬自 agent.py L3430-3441：send_private
# ------------------------------------------------------------------
def send_private(user_id, text, reply_to=None):
    """发私聊。

    reply_to: 要引用回复的消息 id。NapCat 支持 message 数组里的 reply 段：
        [{"type":"reply","data":{"id":"<message_id>"}}, {"type":"text",...}]
    实测返回 {"status":"ok","retcode":0,"data":{"message_id":...}}。
    """
    if reply_to:
        msg = _reply_segment(reply_to)
        msg.append({"type": "text", "data": {"text": text}})
        return napcat("send_private_msg", user_id=user_id, message=msg)
    return napcat("send_private_msg", user_id=user_id, message=text)


# ------------------------------------------------------------------
# 搬自 agent.py L3444-3448：send_private_segments
# ------------------------------------------------------------------
def send_private_segments(user_id, segments, reply_to=None):
    """发私聊，message 段数组（用来发表情包）。reply_to 存在时把引用段插到最前。"""
    segs = _reply_segment(reply_to)
    segs.extend(segments or [])
    return napcat("send_private_msg", user_id=user_id, message=segs)


# ------------------------------------------------------------------
# 搬自 agent.py L3452-3458：_tts_available
# ------------------------------------------------------------------
def _tts_available():
    """本机有没有 edge-tts。没有就返回 None，调用方打一条日志就完事。"""
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return None


# ------------------------------------------------------------------
# 搬自 agent.py L3461-3475：tts_pick
# ------------------------------------------------------------------
def tts_pick(text):
    """挑一段值得转语音的话。返回要念的字符串或空串。"""
    t = clean_reply(text or "").strip()
    if not t:
        return ""
    try:
        mx = int(_live_cfg("tts_max_chars", 60) or 60)
    except Exception:
        mx = 60
    if len(t) > mx:
        return ""
    # 只有标点/表情的不念
    if not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", t):
        return ""
    return t


# ------------------------------------------------------------------
# 搬自 agent.py L3478-3521：tts_say
# ------------------------------------------------------------------
def tts_say(text, voice=""):
    """把一句话合成为 mp3，返回文件路径；失败返回空串（不抛异常）。"""
    t = tts_pick(text)
    if not t:
        return ""
    if not _tts_available():
        log("[语音] 没装 edge-tts（pip install edge-tts），这次跳过", "DEBUG")
        return ""
    try:
        import asyncio
        import edge_tts
        # v1.2.1：合成出来的 mp3 落哪。默认跟着数据目录；如果用户把
        # 「设置 → 语音 → 语音模型目录」改到 D:/E: 盘，这里也跟着走
        # （不然那个设置就成了摆设）。目录建不出来就退回数据目录。
        _md = str(_live_cfg("tts_model_dir", "") or "").strip().strip('"')
        d = _md or os.path.join(cfg_mod.DATA_DIR, "tts")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = os.path.join(cfg_mod.DATA_DIR, "tts")
            os.makedirs(d, exist_ok=True)
        out = os.path.join(d, f"tts_{int(time.time() * 1000)}.mp3")
        v = (voice or _live_cfg("tts_voice", "zh-CN-XiaoxiaoNeural")
             or "zh-CN-XiaoxiaoNeural")

        async def _run():
            # v1.8.5：默认音色太"播音腔"，加一点语速和音调偏移，听起来更像正常语聊。
            # 两个值都能在设置里改（tts_rate / tts_pitch），写空就按默认。
            _rate = str(_live_cfg("tts_rate", "+5%") or "+5%").strip()
            _pitch = str(_live_cfg("tts_pitch", "-2Hz") or "-2Hz").strip()
            try:
                com = edge_tts.Communicate(t, v, rate=_rate, pitch=_pitch)
            except TypeError:
                # 老版本 edge-tts 不认这两个参数，退回原样（不能因为调音就哑巴）
                com = edge_tts.Communicate(t, v)
            await com.save(out)

        asyncio.run(_run())
        if os.path.isfile(out) and os.path.getsize(out) > 1024:
            return out
        log("[语音] 合成出来的文件不正常，放弃发送", "WARN")
    except Exception as e:
        log(f"[语音] 合成失败（忽略）: {e}", "WARN")
    return ""


# ------------------------------------------------------------------
# 搬自 agent.py L3524-3537：send_voice
# ------------------------------------------------------------------
def send_voice(user_id=None, group_id=None, path=""):
    """把 mp3 当语音发出去。NapCat 的 record 段直接给本地绝对路径。"""
    if not path:
        return False
    seg = [{"type": "record", "data": {"file": path}}]
    try:
        if group_id:
            send_group_segments(group_id, seg)
        else:
            send_private_segments(user_id, seg)
        return True
    except Exception as e:
        log(f"[语音] 发送失败（忽略）: {e}", "DEBUG")
        return False


# ------------------------------------------------------------------
# 搬自 agent.py L3543-3543：_LAST_USER_TEXT
# ------------------------------------------------------------------
_LAST_USER_TEXT = {"t": "", "ts": 0.0}


# ------------------------------------------------------------------
# 搬自 agent.py L3546-3550：_TTS_CMD_RE
# ------------------------------------------------------------------
_TTS_CMD_RE = re.compile(
    r"(发[条个段]?语音|来[条个段]语音|语音(回|说|听|来)|说[句句两]?话|说句话|"
    r"唱[首个段]?歌|唱一[段首]|哼一[段下]|念一[段下]|读一[段下]|用语音|语音回复|"
    r"给我听[听一下]*|让我听[听一下]*)"
)


# ------------------------------------------------------------------
# 搬自 agent.py L3562-3578：_tts_wanted_by_user
# ------------------------------------------------------------------
def _tts_wanted_by_user():
    """对方最近 3 分钟里有没有明确要语音。"""
    try:
        if time.time() - float(_LAST_USER_TEXT.get("ts") or 0) > 180:
            return False
        txt = _LAST_USER_TEXT.get("t") or ""
        if not txt:
            return False
        custom = str(_live_cfg("tts_cmd_prompt", "") or "").strip()
        if custom:
            # 用户自己写的触发词，支持 | 分隔，也支持直接写一整句
            for part in [p.strip() for p in custom.replace("，", "|").replace(",", "|").split("|") if p.strip()]:
                if part in txt:
                    return True
        return bool(_TTS_CMD_RE.search(txt))
    except Exception:
        return False


# ------------------------------------------------------------------
# 搬自 agent.py L3581-3583：tts_mode
# ------------------------------------------------------------------
def tts_mode():
    m = str(_live_cfg("tts_mode", "auto") or "auto").strip().lower()
    return m if m in ("auto", "always", "command") else "auto"


# ------------------------------------------------------------------
# 搬自 agent.py L3586-3618：tts_prepare
# ------------------------------------------------------------------
def tts_prepare(user_id="", group_id="", text=""):
    """v1.4.0：按「语音三档」决定这次要不要发语音，并【先把 mp3 合成好】。

    返回合成好的文件路径；空串表示这次不发。
    为什么拆成「准备」和「发送」两步：以前是先发完文字再补一条语音，
    对方会先看到一屏字、过一会儿才听到声音，一点不像真人打电话说话。
    现在要能「语音先到、文字后到」。
    """
    try:
        if not bool(_live_cfg("tts_enabled", False)):
            return ""
        scope = str(_live_cfg("tts_scope", "private") or "private")
        if group_id and scope != "all":
            return ""
        mode = tts_mode()
        if mode == "command":
            # 喊了才发：他没要，就不发
            if not _tts_wanted_by_user():
                log("[语音] 这次没发：当前是「喊了才发」，他刚才没要语音", "DEBUG")
                return ""
        elif mode == "auto":
            # 偶尔发：按概率来（默认 0.2，可调）
            try:
                prob = float(_live_cfg("tts_prob", 0.2) or 0.2)
            except Exception:
                prob = 0.2
            if random.random() > max(0.0, min(1.0, prob)):
                return ""
        # mode == "always"：每条都发，不拦
        return tts_say(text) or ""
    except Exception as e:
        log(f"[语音] 准备失败（忽略）: {e}", "DEBUG")
        return ""


# ------------------------------------------------------------------
# 搬自 agent.py L3621-3632：tts_send
# ------------------------------------------------------------------
def tts_send(user_id="", group_id="", path=""):
    """把 tts_prepare() 合成好的语音发出去。"""
    if not path:
        return False
    try:
        ok = send_voice(user_id=user_id, group_id=group_id, path=path)
        if ok:
            log(f"[语音] 已发出一条语音：{os.path.basename(path)}")
        return ok
    except Exception as e:
        log(f"[语音] 发送失败（忽略）: {e}", "DEBUG")
        return False


# ------------------------------------------------------------------
# 搬自 agent.py L3635-3638：maybe_tts
# ------------------------------------------------------------------
def maybe_tts(user_id="", group_id="", text=""):
    """兼容旧调用：合成后立刻发（语音在文字之后）。新代码请用 tts_prepare/tts_send。"""
    return tts_send(user_id=user_id, group_id=group_id,
                    path=tts_prepare(user_id=user_id, group_id=group_id, text=text))


# ------------------------------------------------------------------
# 搬自 agent.py L3641-3648：send_group
# ------------------------------------------------------------------
def send_group(group_id, text, at_user=None, reply_to=None):
    segs = _reply_segment(reply_to)
    if at_user:
        segs.append({"type": "at", "data": {"qq": str(at_user)}})
        segs.append({"type": "text", "data": {"text": " " + text}})
    else:
        segs.append({"type": "text", "data": {"text": text}})
    return napcat("send_group_msg", group_id=group_id, message=segs)


# ------------------------------------------------------------------
# 搬自 agent.py L3651-3657：send_group_segments
# ------------------------------------------------------------------
def send_group_segments(group_id, segments, at_user=None, reply_to=None):
    segs = _reply_segment(reply_to)
    if at_user:
        segs.append({"type": "at", "data": {"qq": str(at_user)}})
        segs.append({"type": "text", "data": {"text": " "}})
    segs.extend(segments or [])
    return napcat("send_group_msg", group_id=group_id, message=segs)


# ------------------------------------------------------------------
# 搬自 agent.py L3660-3695：send_group_human
# ------------------------------------------------------------------
def send_group_human(group_id, text, at_user=None, reply_to=None, no_split=False):
    # v1.9.12 补丁：模型可能用 [[APPLY_AVATAR]] 之类标记表达「我真的换了」
    text, _acts = _take_profile_marks(text)
    if _acts:
        _apply_profile_marks("group_" + str(group_id), _acts)
    if not str(text or "").strip():
        return True
    # v1.8.5：语音和文字互斥 —— 语音真的发出去了就不再补一屏文字。
    # 以前是「先发语音、再把同一句话逐段打出来」，用户收到两遍，像复读机；
    # 现在只有合成失败或发送失败才降级成文字。
    _voice_ok = False
    try:
        _vp = tts_prepare(group_id=group_id, text=text)
        if _vp:
            _voice_ok = bool(tts_send(group_id=group_id, path=_vp))
            if _voice_ok:
                time.sleep(random.uniform(0.5, 1.2))
    except Exception as _e:
        log(f"[语音] 群 {group_id} 语音没成，这次改发文字：{_e}", "DEBUG")
        _voice_ok = False
    if _voice_ok:
        log(f"[语音] 群 {group_id} 已发出语音，按互斥规则不再发文字", "DEBUG")
        followup_note("group_" + str(group_id), text, uid="")
        return
    # QA P2-1：主动分享这类「一句话」的消息不拆条
    parts = [text] if no_split else split_reply(text)
    for i, p in enumerate(parts):
        if i > 0:
            time.sleep(random.uniform(1.0, 3.0))
        send_group(group_id, p,
                   at_user=at_user if i == 0 else None,
                   reply_to=reply_to if i == 0 else None)
    # v1.8.11：所有真正发出去的话都在这里登记「等回话」。
    # v1.8.10 把登记放在 send_to_session 里，可普通回复根本不走那个函数
    # （全是对 send_group_human 直调），所以「对方不回就追问」一次都没触发过。
    followup_note("group_" + str(group_id), text, uid="")


# ------------------------------------------------------------------
# 搬自 agent.py L3698-3726：send_private_human
# ------------------------------------------------------------------
def send_private_human(user_id, text, reply_to=None, no_split=False):
    # v1.9.12 补丁：同 send_group_human，先把控制标记摘出来执行掉
    text, _acts = _take_profile_marks(text)
    if _acts:
        _apply_profile_marks("private_" + str(user_id), _acts)
    if not str(text or "").strip():
        return True
    # v1.8.5：同 send_group_human —— 语音发出去就不再补文字。
    _voice_ok = False
    try:
        _vp = tts_prepare(user_id=user_id, text=text)
        if _vp:
            _voice_ok = bool(tts_send(user_id=user_id, path=_vp))
            if _voice_ok:
                time.sleep(random.uniform(0.5, 1.2))
    except Exception as _e:
        log(f"[语音] {user_id} 语音没成，这次改发文字：{_e}", "DEBUG")
        _voice_ok = False
    if _voice_ok:
        log(f"[语音] {user_id} 已发出语音，按互斥规则不再发文字", "DEBUG")
        followup_note("private_" + str(user_id), text, uid=str(user_id))
        return
    parts = [text] if no_split else split_reply(text)
    for i, p in enumerate(parts):
        if i > 0:
            time.sleep(random.uniform(1.0, 3.0))
        send_private(user_id, p, reply_to=reply_to if i == 0 else None)
    # v1.8.11：私聊每条发出去的话都登记「等回话」（见群聊那段注释）
    followup_note("private_" + str(user_id), text, uid=str(user_id))


# ------------------------------------------------------------------
# 搬自 agent.py L3731-3739：followup_note
# ------------------------------------------------------------------
def followup_note(session_key, text="", uid=""):
    """登记一条「等回话」（v1.8.11）。

    任何异常都不许影响发消息 —— 追问只是锦上添花。
    """
    try:
        _g.call("followup_mark", session_key, text, uid=uid)
    except Exception as _fe:
        log(f"[追问] 登记待回话失败（忽略）: {_fe}", "DEBUG")


# ------------------------------------------------------------------
# 搬自 agent.py L3773-3779：_data_path
# ------------------------------------------------------------------
def _data_path(name):
    """配置里的目录名 -> 绝对路径（相对名按数据目录解析）"""
    if not name:
        return DATA_DIR
    if os.path.isabs(str(name)):
        return str(name)
    return os.path.join(DATA_DIR, str(name))


# ------------------------------------------------------------------
# 搬自 agent.py L3782-3787：_sticker_path_cfg
# ------------------------------------------------------------------
def _sticker_path_cfg():
    """表情包目录：配置里可以是绝对路径，也可以是相对数据目录的名字"""
    d = _g.live("STICKER_DIR_CFG", "")
    if not d:
        return None
    return _data_path(d)


# ------------------------------------------------------------------
# 搬自 agent.py L3790-3822：send_sticker
# ------------------------------------------------------------------
def send_sticker(session_key, text="", emotions=None, reply_to=None):
    """按语境挑一个表情包发出去。返回是否发出去了。"""
    if not _g.live("STICKER_ENABLED", False):
        return False
    try:
        import stickers as sticker_mod
    except Exception as e:
        log(f"[表情包] 模块加载失败: {e}", "WARN")
        return False
    try:
        d = _sticker_path_cfg()
        sticker_mod.ensure_dir(d)
        pick = sticker_mod.pick_sticker(text=text, emotions=emotions, cfg_dir=d)
        if not pick:
            log("[表情包] 没有匹配的表情包，跳过", "DEBUG")
            return False
        seg = sticker_mod.sticker_to_segment(pick["path"])
        kind, target = parse_session_key(session_key)
        if kind == "private":
            r = send_private_segments(target, [seg], reply_to=reply_to)
        elif kind == "group":
            r = send_group_segments(target, [seg])
        else:
            return False
        ok = bool(r and r.get("status") == "ok")
        if ok:
            log(f"[表情包] 已发送 {pick['name']} (标签 {pick['tags']})", "INFO")
        else:
            log(f"[表情包] 发送失败: {str(r)[:200]}", "WARN")
        return ok
    except Exception as e:
        log(f"[表情包] 发送异常: {type(e).__name__}: {e}", "ERROR")
        return False


# ------------------------------------------------------------------
# 搬自 agent.py L3825-3848：send_image_file
# ------------------------------------------------------------------
def send_image_file(session_key, path, reply_to=None):
    """把一张本地图片当表情发出去（生图结果走这条路）。"""
    try:
        seg = stickers_mod.sticker_to_segment(path)
    except Exception as e:
        log(f"[生图] 组装图片段失败: {e}", "WARN")
        return False
    kind, target = parse_session_key(session_key)
    try:
        if kind == "private":
            r = send_private_segments(target, [seg], reply_to=reply_to)
        elif kind == "group":
            r = send_group_segments(target, [seg])
        else:
            return False
    except Exception as e:
        log(f"[生图] 发送异常: {e}", "ERROR")
        return False
    ok = bool(r and r.get("status") == "ok")
    if ok:
        log(f"[生图] 已发送 {os.path.basename(path)}", "INFO")
    else:
        log(f"[生图] 发送失败: {str(r)[:200]}", "WARN")
    return ok


# ------------------------------------------------------------------
# 搬自 agent.py L3903-3911：parse_session_key
# ------------------------------------------------------------------
def parse_session_key(key):
    """'private_123' -> ('private','123')；'group_456' -> ('group','456')"""
    if not key:
        return (None, None)
    if key.startswith("private_"):
        return ("private", key[len("private_"):])
    if key.startswith("group_"):
        return ("group", key[len("group_"):])
    return (None, None)


# ------------------------------------------------------------------
# 搬自 agent.py L3914-3936：send_to_session
# ------------------------------------------------------------------
def send_to_session(session_key, text, at_user=None, reply_to=None, no_split=False,
                    remember=False):
    """按 session_key 自动选择私聊/群聊发送。返回是否成功。

    v1.9.12 任务三：`remember=True` 时把这句话写进历史（role=assistant）。
    主动分享/主动开口必须传它 —— 否则她「发完就忘」，过几轮问起视频就装傻。
    """
    kind, target = parse_session_key(session_key)
    if kind == "private":
        send_private_human(target, text, reply_to=reply_to, no_split=no_split)
        # v1.8.11：登记动作已经下沉到 send_private_human / send_group_human，
        # 这里不再重复登记（否则同一条消息会登记两次）。
        if remember:
            remember_assistant(session_key, text)
        return True
    if kind == "group":
        send_group_human(target, text, at_user=at_user, reply_to=reply_to,
                         no_split=no_split)
        if remember:
            remember_assistant(session_key, text)
        return True
    log(f"无法识别的 session_key: {session_key}", "WARN")
    return False


# ------------------------------------------------------------------
# 搬自 agent.py L5473-5475：_PROFILE_MARK_RE
# ------------------------------------------------------------------
_PROFILE_MARK_RE = re.compile(
    r"\[\[\s*(APPLY_AVATAR|APPLY_NAME|APPLY_PROFILE)\s*(?:[:：]\s*([^\[\]]{1,40}))?\s*\]\]",
    re.I)


# ------------------------------------------------------------------
# 搬自 agent.py L5476-5476：_LAST_IMAGES
# ------------------------------------------------------------------
_LAST_IMAGES = {}


# ------------------------------------------------------------------
# 搬自 agent.py L5487-5497：_take_profile_marks
# ------------------------------------------------------------------
def _take_profile_marks(text):
    """从模型回复里取走控制标记，返回 (干净文本, 动作列表)。"""
    acts = []

    def _sub(m):
        acts.append({"kind": (m.group(1) or "").upper(),
                     "arg": (m.group(2) or "").strip()})
        return ""

    clean = _PROFILE_MARK_RE.sub(_sub, text or "")
    return clean.strip(), acts


# ------------------------------------------------------------------
# 搬自 agent.py L5500-5531：_apply_profile_marks
# ------------------------------------------------------------------
def _apply_profile_marks(session_key, acts):
    """按模型的决定真的换头像/改名。冷却、失败都只记日志，绝不打断回复。"""
    if not acts:
        return ""
    try:
        import selfmod as _smod
    except Exception as e:
        log("[换资料] selfmod 不可用：%s" % e, "WARN")
        return ""
    imgs = _LAST_IMAGES.get(str(session_key or "")) or []
    done = []
    for a in list(acts)[:2]:
        kind = a.get("kind")
        arg = a.get("arg") or ""
        try:
            if kind in ("APPLY_AVATAR", "APPLY_PROFILE") and imgs:
                r = _smod.update_profile(avatar_path=imgs[0])
            elif kind == "APPLY_NAME" and arg:
                r = _smod.update_profile(nickname=arg)
            else:
                log("[换资料] 缺图或缺名字，跳过 %s" % kind, "WARN")
                continue
        except Exception as e:
            log("[换资料] 执行异常（忽略）：%s" % e, "WARN")
            continue
        if (r or {}).get("ok"):
            done.append(kind)
            log("[换资料] 已按模型决定执行：%s%s"
                % (kind, (" - " + arg) if arg else ""), "INFO")
        else:
            log("[换资料] 没换成：%s" % str((r or {}).get("msg") or "")[:90], "WARN")
    return ",".join(done)


# ------------------------------------------------------------------
# 搬自 agent.py L6335-6351：remember_assistant
# ------------------------------------------------------------------
def remember_assistant(key, text, images=None):
    """她主动说/主动发出去的东西，也要进历史 —— 否则过几轮就「装傻」。"""
    try:
        t = (text or "").strip()
        if not t:
            return False
        _g.live("sessions").add(key, "assistant", t)
        try:
            if _g.live("MEMORY_ENABLED", True):
                _mem = _g.live("mem")
                _mem.add_message(key, "assistant", t)
                _mem.log_message(key, "小鲸鱼", "assistant", t)
        except Exception as e:
            log("[记忆] 主动消息落库失败（忽略）：%s" % e, "DEBUG")
        return True
    except Exception as e:
        log("[记忆] 主动消息记历史失败（忽略）：%s" % e, "DEBUG")
        return False




# ---------------------------------------------------------------
# 说明：这里没有 `import agent`，需要 agent 里的活名字一律走 core.globals，
# 否则 agent ↔ core.sender 会形成循环引用。
# ---------------------------------------------------------------
