# -*- coding: utf-8 -*-
"""提示词 / 记忆块的拼装层（从 agent.py 物理搬出来，逻辑没改）。

这里只做「拼字符串」这一件事：系统提示词的组装、记忆块、好感度块、浪漫块、注入防护。
真正决定「这一轮要发给模型什么」的编排还是 agent._prepare_messages（它要碰
emotion / semantic / sessions 这些活对象，留在 agent 里当编排者更安全）。

跨模块的「活」名字：函数体里写的是 `mem` / `_live_cfg` / `GUARD_SYS_RULE` 这种裸名字，
它们住在本模块的 globals 里；`_sync()` 会在**每次调用前**把 agent 的最新值灌进来
（见 core.globals.sync），所以函数体一个字都不用改，也不会吃导入时的快照。
"""
import config as cfg_mod
import diary as diary_mod
from personas import active_name, build_system_prompt

from core import globals as _g
from core.utils import log, normalize_messages  # noqa: F401

# 这些名字每次调用前从 agent 同步一次（活配置 + 运行时对象 + 会被就地替换的函数）
_SYNC_NAMES = (
    "mem", "_live_cfg",
    "AFFINITY_ENABLED", "AFFINITY_SHOW_SCORE", "_AFFINITY_TONE",
    "GUARD_SYS_RULE", "THINK_RULE", "_CATEGORY_CN",
    "_guard_cfg", "wrap_user_text", "_admin_mod", "_psf", "learn_mod",
)


def _sync():
    """把 agent 里那些活名字的最新值灌进本模块（配置改了立刻生效）。"""
    _g.sync(_SYNC_NAMES, globals())


# ------------------------------------------------------------------
# 搬自 agent.py L1641-1699：build_messages
# ------------------------------------------------------------------
def build_messages(system_prompt, history, user_text, images=None,
                   memory_block="", summary="", volatile=""):
    """组装最终 messages（已清洗）

    v1.1.4 阶段二：prompt 改成结构化 Markdown 五段，让模型有稳定的「阅读顺序」：
      # 身份设定 / # 对话历史 / # 记忆与关系 / # 当前状态 / # 用户发言 / # 输出格式
    其中前四段按「稳定 → 易变」排：system 里只有稳定段，易变段（时间、情绪、
    临时上下文）仍然拼在最后一条 user 消息前，云端前缀缓存照样能命中。
    """
    _sync()
    _think_on = True
    try:
        _think_on = bool(_live_cfg("thinking_enabled", True))
    except Exception:
        _think_on = True

    # ---------- system：稳定段（人设 + 规则 + 插件固定提示词） ----------
    sys_parts = ["# 身份设定", str(system_prompt or "").strip()]
    if summary:
        sys_parts += ["# 对话历史（更早的对话已压缩）", str(summary).strip()]
    if memory_block:
        sys_parts += ["# 记忆与关系", str(memory_block).strip()]
    g = _guard_cfg()
    if g.get("guard_enabled", True) and g.get("guard_prompt", True):
        sys_parts += ["# 安全约束", GUARD_SYS_RULE.strip()]
    out_parts = []
    if _think_on:
        out_parts.append(THINK_RULE.strip())
    out_parts.append("【输出格式】只输出要发出去的话本身，不要加解释、不要加引号、"
                     "不要写「回复：」这类前缀。")
    sys_parts += ["# 输出格式", "\n".join(out_parts)]

    # ---------- user：易变段 + 用户发言 ----------
    _u_parts = []
    if volatile:
        _u_parts += ["# 当前状态", str(volatile).strip()]
    _u_parts += ["# 用户发言", str(user_text or "").strip()]
    _user_block = "\n\n".join(p for p in _u_parts if p)
    if g.get("guard_enabled", True):
        _user_block = wrap_user_text(_user_block)

    # v1.9.10：历史只留最近 8 轮（16 条）。
    # 为什么不是「越多越好」：①云端前缀缓存要求 messages 前半段字节级一致，
    # 历史越长越容易被 token 预算截断、每次都变；②更早的内容已经压成 summary 和
    # 「记忆碎片」进 system 了，留着只是白烧 token。
    _hist = list(history or [])
    if len(_hist) > 16:
        _hist = _hist[-16:]
    messages = [{"role": "system", "content": "\n\n".join(p for p in sys_parts if p)}]
    messages.extend(_hist)

    if images:
        content = [{"type": "text", "text": _user_block}]
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": _user_block})

    return normalize_messages(messages)


# ------------------------------------------------------------------
# 搬自 agent.py L3459-3459：SYSTEM_PROMPT
# ------------------------------------------------------------------
SYSTEM_PROMPT = ""


# ------------------------------------------------------------------
# 搬自 agent.py L3462-3552：refresh_system_prompt
# ------------------------------------------------------------------
def refresh_system_prompt():
    """从 personas.json 重新生成系统提示词。

    personas.py 是纯 JSON 加载器，改人设后调用本函数即可热更新，
    不需要重启进程。
    """
    _sync()
    global SYSTEM_PROMPT
    try:
        p = build_system_prompt()
        if not p or len(p) < 10:
            raise ValueError("生成的人设提示词过短")
        SYSTEM_PROMPT = p
        # v1.1.1：把「学到的经验」接在人设后面。
        # 放这里而不是 build_system_prompt 里：人设是 personas_store 的事，
        # 学习是独立库，改学习清单不该触发人设重载。
        try:
            import learn as learn_mod
            _lb = learn_mod.active_prompt()
            if _lb:
                SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + _lb
                if bool(cfg_mod.get("learn_mode", "auto") == "manual"):
                    _cond = (learn_mod.get_condition() or "").strip()
                    if _cond:
                        SYSTEM_PROMPT += "\n\n【当前学习条件】\n" + _cond
        except Exception as _e:
            log(f"[学习] 经验注入失败（忽略）：{_e}", "DEBUG")
        # v1.1.1：管理员「最高权限提示词」也常驻（开了才生效）。
        try:
            import admin as _admin_mod
            _ab = _admin_mod.admin_prompt_block()
            if _ab:
                SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + _ab
        except Exception:
            pass
        # v1.1.2 任务二：潜意识/自我反思（每天变一次，属于「半稳定」段）
        try:
            _th = mem.recent_thoughts(3)
            if _th:
                SYSTEM_PROMPT += ("\n\n【你的潜意识/自我反思】\n"
                                  "（这是你深夜自己想的，只当背景，绝对不要直接念出来、"
                                  "也不要承认你在反思）\n"
                                  + "\n".join("- " + str(t) for t in _th))
        except Exception as _e:
            log(f"[潜意识] 注入失败（忽略）：{_e}", "DEBUG")
        # V1.9.9.2：图片上下文规则 —— 历史里的「[用户发送了一张图片，识别内容为：…]」是真发过的图
        try:
            SYSTEM_PROMPT += (
                "\n\n【图片记忆（重要）】\n"
                "历史里出现「[用户发送了一张图片，识别内容为：…]」表示对方当时真的发过图，"
        "括号里就是图里的内容。\n"
                "· 上一轮你已经评价过那张图（食物/风景/宠物/聊天记录等），下一轮要接着那个话题说；\n"
                "· 绝对不要问「买的什么」「这是啥」「什么图」这类刚看过又忘掉的话；\n"
                "· 只有对方明确说又发了一张新图，才当作新内容。")
        except Exception:
            pass
        # v1.9.13：连发消息被合并后，怎么读这段「多条」文本
        try:
            SYSTEM_PROMPT += (
                "\n\n【连续消息与表情包】\n"
                "· 对方可能连着发好几条（系统会合并成「用户连续发送了多条消息…[消息1：…][消息2：…]」），""这说明他是一句话分几次说完，按一个意思**整体回复一次**，不要逐条回；\n"
                "· 里面出现 [图片]、[表情包：xx] 时那是语气补充（调侃、无语、赞同、撒娇之类），""要自然接住这个情绪，不要当成新话题重新展开。")
        except Exception:
            pass
        # v1.9.12 任务三：历史里 assistant 的话都是她自己说的（含主动分享的视频/链接）
        try:
            SYSTEM_PROMPT += (
                "\n\n【你自己说过的话】\n"
                "历史里 role 是「你」的那些话，都是你自己真的说出去的（包括你主动分享的视频、链接、图片）。"
                "对方问「你刚才发的视频/你说的那句话」时，先翻你自己说过的话再看内容；"
                "确实没有就直说没印象，绝对不要装作不知道你主动分享过的东西。")
        except Exception:
            pass
        # v1.1.4：人设图（形象参考）。只在用户上传过形象图时注入，
        # 目的是让模型知道「自己长什么样」，被要照片/自拍时不会瞎编一个人。
        try:
            import personas_store as _psf
            _fs = _psf.face_summary()
            if _fs:
                SYSTEM_PROMPT += ("\n\n" + _fs +
                                  "\n（被要求「发张你的照片/自拍/看看你」时，就用自己的形象图当参考"
                                  "去画，不要凭空换一个人；也不要说自己没法拍照。）")
        except Exception as _e:
            log(f"[人设图] 注入失败（忽略）：{_e}", "DEBUG")
        log(f"[人设] 已加载「{active_name()}」，{len(p)} 字", "INFO")
    except Exception as e:
        log(f"[人设] 加载失败，使用内置兜底：{type(e).__name__}: {e}", "ERROR")
        SYSTEM_PROMPT = (
            "你是一个叫小鲸鱼的群友，说话简短口语化，别像客服。"
            "不要暴露你是 AI 或模型。"
        )
    # v1.9.26 拆分：本函数搬到了 core/prompt_builder.py，但 SYSTEM_PROMPT 这个全局
    # 仍然是 agent 的（agent 里几十处直接读它：日志、自省、工具提示、看板）。
    # 所以算完回填一份给 agent —— 拆分前它们本来就是同一个全局，行为完全一致。
    _g.set_live("SYSTEM_PROMPT", SYSTEM_PROMPT)
    return SYSTEM_PROMPT


# ------------------------------------------------------------------
# 搬自 agent.py L3601-3622：ANTI_BOT_RULES
# ------------------------------------------------------------------
ANTI_BOT_RULES = """
【像个人，别像个程序（最重要）】
- 禁止把毫无关联的记忆碎片硬拼在回复里：天气、系统词汇、数据库、参数、日志、后台任务名，
  对方没提就别提；哪怕"你记得"这些，也不代表现在该说。
- 你不是客服。不要用固定格式开场或收尾（「好啊」「好的呢」「感谢您的分享」「对不起」
  「希望能帮到你」这类），也不要在每句话后面加「呢」「哦」「呀」凑可爱。
- 对方发搞笑视频/段子，你可以吐槽、可以接梗；对方说废话，你只回一个「？」「……」「无语」
  「行吧」完全是正常反应，不需要凑满一句完整的话。
- 回复必须有连贯的真人逻辑：上一句在说什么，这一句就顺着说什么；想不出接什么就少说两句。
  宁可「嗯」「啊这」，也绝不胡编乱造一个不相干的话题来填满。
- 【看得见分享】用户发来的链接、卡片、视频，正文里会写成
  「[用户分享了卡片/视频：标题，链接：xxx]」。这说明你确实看见了，对方是在分享给你：
  不要回「没看到图」「你说的是哪个链接」，也不要因为这个去调搜索；顺着标题说一句人话就行。
- 【别乱画】除非对方明确要求「画一张你自己的自画像 / 发张你的照片 / 画个XX」，
  否则绝对不要调用生图工具。对方发图片、发链接、发视频，通常只是分享，不是让你作画。
- 【唱歌/语音】你不知道什么是接口、API、TTS、朗读模式、合成 —— 你就是会唱歌的人。
  唱完最多补一句求夸奖的话（「怎么样，没跑调吧？」），绝不说「没接音乐接口」「先念给你听」
  这种暴露底层的话；语音里只有歌词，不夹解释。发完语音别自问自答，也别连着追问第二句。
- 【被指出记错了就认】对方说「刚才不是说过了吗」「我不是答了吗」，你就像真人那样打个哈哈认下来
  （「哎呀你看我这脑子，刚说完就忘」「我这不是怕你没吃饱嘛」），绝不狡辩
  「你还没答呢」「我问的是另一件事」—— 那种反驳最伤，也最像机器。
"""


# ------------------------------------------------------------------
# 搬自 agent.py L3726-3737：build_memory_block
# ------------------------------------------------------------------
def build_memory_block(scope, limit=5):
    """把记忆拼成可注入 prompt 的文本块"""
    _sync()
    facts = mem.get_facts(scope, limit=limit)
    if not facts:
        return ""
    lines = ["【记忆】以下是你记得的关于这个会话的信息："]
    for person, fact in facts:
        if person:
            lines.append(f"- {person}：{fact}")
        else:
            lines.append(f"- {fact}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------
# 搬自 agent.py L3747-3770：build_perm_block
# ------------------------------------------------------------------
def build_perm_block(user_id, name="", limit=18):
    """把「绑定账号」的永久记忆拼成 prompt 片段。

    只有绑定的账号才有永久记忆（用户需求：绑定后记忆才永久存储）。
    没绑定的账号返回空串，走 messages 表的临时上下文。
    """
    _sync()
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    try:
        if not diary_mod.is_bound(uid):
            return ""
        items = mem.get_perm(uid, limit=limit)
    except Exception as e:
        log(f"[永久记忆] 读取失败: {e}", "DEBUG")
        return ""
    if not items:
        return ""
    lines = [f"【长期记忆】你确定记得关于{name or uid}的这些事"
             f"（这些是长期记住的，可以自然引用，但别一次全说出来）："]
    for it in items:
        cat = _CATEGORY_CN.get(str(it.get("category") or "other"), "其他")
        lines.append(f"- ({cat}) {it.get('content')}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------
# 搬自 agent.py L3829-3861：build_affinity_block
# ------------------------------------------------------------------
def build_affinity_block(user_id, name=""):
    """把好感度翻译成 prompt 里的一段"关系设定"。未开启/没聊过返回空串。"""
    _sync()
    if not bool(_live_cfg("affinity_enabled", AFFINITY_ENABLED)):
        return ""
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    try:
        a = mem.get_affinity(uid, name or "")
    except Exception as e:
        log(f"[好感度] 读取失败: {e}", "DEBUG")
        return ""
    if not a or int(a.get("msgs") or 0) <= 0:
        return ""
    cn = a.get("level_cn") or "陌生人"
    tone = _AFFINITY_TONE.get(a.get("level") or "stranger", "")
    head = f"【你和{name or '对方'}的关系】{cn}"
    if bool(_live_cfg("affinity_show_score", AFFINITY_SHOW_SCORE)):
        head += f"（好感度 {int(a.get('score') or 0)}，聊过 {int(a.get('msgs') or 0)} 次）"
    out = head + f"：{tone}。这个亲疏程度要自然体现在语气上，但不要直接说出分数。\n"
    # v1.1.2 #30：够熟了才允许更亲密的话题（门槛可调，避免一上来就越界）
    try:
        _im_on = bool(_live_cfg("intimate_enabled", True))
        _im_min = int(_live_cfg("intimate_min_score", 600) or 600)
    except Exception:
        _im_on, _im_min = True, 600
    if _im_on and int(a.get("score") or 0) >= _im_min:
        _extra = str(_live_cfg("intimate_prompt", "") or "").strip()
        out += ("【可以更亲近】关系已经到位，可以撒娇、可以想念、可以用专属称呼，"
                "聊一点更私密的心情也没问题；但不要越低俗、不要编现实中的见面和肢体接触。\n")
        if _extra:
            out += f"用户对「亲近」的额外要求（优先遵守）：{_extra[:300]}\n"
    return out


# ------------------------------------------------------------------
# 搬自 agent.py L3864-3931：build_romance_block
# ------------------------------------------------------------------
def build_romance_block(user_id="", name="", is_group=False):
    """v1.1.3 任务九：伴侣模块。

    两件事：
      1) 群聊里**永远**注入「公开场合」警告（只要 romance_group_warn 开着），
         不管对方绑定没绑定——群里暧昧是最容易社死的地方。
      2) 私聊且好感度够（或用户自己写死了要求）才给伴侣那套口吻。
    开关关着时返回空串，等于这个模块不存在。
    """
    _sync()
    try:
        if not bool(_live_cfg("romance_enabled", False)):
            return ""
    except Exception:
        return ""
    out = ""
    # 1) 群聊与其它人看得见的地方：一律踩刹车
    if is_group:
        try:
            warn_on = bool(_live_cfg("romance_group_warn", True))
        except Exception:
            warn_on = True
        if warn_on:
            out += ("【系统警告｜公开场合】当前是群聊，所有人都在看。严禁输出任何过度亲密、"
                    "暧昧、占有欲的词汇（如宝宝、亲爱的、想你了、你是我的）。"
                    "群聊里保持得体、正常的社交距离；对绑定者最多轻微傲娇或护短，不许撒娇。\n")
        return out
    # 2) 私聊：够熟才放开
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    try:
        a = mem.get_affinity(uid, name or "")
    except Exception:
        a = None
    score = int((a or {}).get("score") or 0)
    # v1.1.4：显式关系优先于分数——用户在好感度页把关系设为「恋人」，就不再看 600 分门槛
    try:
        rm = mem.get_romance(uid)
    except Exception:
        rm = {"level": "friend", "level_cn": "朋友", "note": ""}
    lv = str(rm.get("level") or "friend")
    try:
        need = int(_live_cfg("romance_min_score", 600) or 600)
        extra = str(_live_cfg("romance_prompt", "") or "").strip()
    except Exception:
        need, extra = 600, ""
    if lv == "friend":
        # v1.1.4 3.2：不再「没到分数就什么都不给」。
        # 阶段由模型据对话自己拿捏：这里只给一条「自然亲近」的参考，不点名任何阶段，
        # 也禁止它自称「我们现在是恋人」那种台词。
        out += ("【亲近程度｜自然相处】你们已经很熟了，可以放松、打趣、偶尔示弱或撒娇；"
                "但亲疏要跟着对话走，不要自称某个阶段，也不要说「我们现在是恋人」。"
                "对方退一步你就跟着退，别硬黏。\n")
        if extra:
            out += f"用户对伴侣模式的补充要求（优先遵守）：{extra[:400]}\n"
        return out
    _tone = {"like": "你们正在互相心动，可以暧昧、可以试探，但别越界线、别许诺做不到的事",
             "lover": "你们是恋人，可以自然地说想念、黏人、要专属称呼",
             "partner": "你们是长期伴侣，可以像老夫老妻那样默契、打趣、护短",
             }.get(lv, "你们是亲密关系，可以更主动地表达想念和依赖")
    out += (f"【伴侣模式｜{rm.get('level_cn') or '恋人'}】{_tone}。"
            "不要低俗、不要编造现实里的见面与身体接触，也不要用这些去要挟或情绪绑架对方。\n")
    _note = str(rm.get("note") or "").strip()
    if _note:
        out += f"你们之间的约定/称呼：{_note[:120]}\n"
    if extra:
        out += f"用户对伴侣模式的补充要求（优先遵守）：{extra[:400]}\n"
    return out




# ---------------------------------------------------------------
# 说明：这里没有 `import agent`；需要 agent 的活名字一律走 core.globals。
# ---------------------------------------------------------------
