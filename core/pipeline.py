# -*- coding: utf-8 -*-
"""消息处理流水线（从 agent.py 物理搬出来，逻辑没改）。

一条消息进来之后要走的全部步骤都在 process_message 里：
  媒体归一化 → 记忆/人设取用 → 决定要不要回（agent_wanted / fast_route）→
  拼上下文（_hist_turns / group_ctx / compress_old_context）→ 调模型（ask_model）→
  清洗与拆分（clean_reply / _split_remember）→ 拟人延迟（human_delay）→
  发出去（send_*_human）→ 事后记账（note_replied_text / mark_replied / 好感度 / 表情包 / 语音）

为什么它在这儿、而不是留在 agent：
  这一步有 500 多行，是「改动最频繁」的那类代码。搬出来以后，动它不用再翻 7000 行。

为什么 agent.py 还留一个 process_message：
  `app_ui.py` / `web_server.py` / 自测都是 `agent.process_message(...)`，包装层保证
  调用点一个字都不用改（拆分三原则之二）。

跨模块的「活」名字：函数体里写的是 `mem` / `sessions` / `_live_cfg` / `ask_model` 这类裸名字，
`_sync()` 在每次调用前把 agent 的最新值灌进来（见 core.globals.sync）——
所以配置改了立刻生效，自测/插件对 agent.xxx 的就地替换也依然有效。
"""
import random
import re
import threading
import time

from core import globals as _g

_SYNC_NAMES = ('CODEWORD_ENABLED', 'CODEWORD_SCOPE', 'EMO_JUDGE_SYSTEM', 'ENABLE_VISION', 'GROUP_CONTEXT_LOOKBACK', 'MEMORY_AUTO_SAVE', 'MEMORY_ENABLED', 'SUMMARY_BUDGET', '_LAST_EMO', '_MEDIA_ONLY_HEAD', '_MERGE_EMPTY', '_emo', '_extract_text', '_fast_review', '_fast_talk_note', '_hist_turns', '_is_temp_session', '_live_cfg', '_memory_text', '_note_user_text', '_perm_extract_job', '_plain_ask_text', '_remember_begin', '_remember_take', '_steal_stickers', '_strip_profile_marks', 'agent_wanted', 'already_replied_same', 'apply_guards', 'ask_model', 'ask_model_raw', 'auto_agent_reply', 'bump_affinity_for', 'check_codeword', 'collect_images', 'compress_old_context', 'dup_reaction', 'extract_and_store', 'extract_events', 'extract_sticker_hints', 'fast_route', 'followup_clear', 'group_ctx', 'human_delay', 'log', 'mark_replied', 'maybe_post_reflect', 'maybe_send_sticker', 'mem', 'note_recent_msg', 'note_replied_text', 'quote_target', 'raise_codeword_alert', 'send_group_human', 'send_private_human', 'sessions', 'set_typing', 'vision_caption')


def _sync():
    """把 agent 里那些活名字的最新值灌进本模块（每处理一条消息都同步一次）。"""
    _g.sync(_SYNC_NAMES, globals())


def process_message(event, mtype, key, sender_name, raw_text, target, user_id):
    """真正处理一条消息（已在会话锁内）"""
    _sync()
    log(f"[{mtype}] {sender_name}: {raw_text[:70]}")
    note_recent_msg(key, event.get("message_id"))   # v1.1.1：算引用间隔用
    # v1.1.2 任务一：从「对方这句话」算一次紧急度，后面所有 human_delay 都用它。
    # 急/难受（urgency>=0.8）时不再假装打字，0.5 秒内回。
    _urg = 0.0
    # v1.3.0：记一下对方这句话，「喊了才发」的语音靠它判断
    # v1.9.12 补丁：控制标记只认模型说的，用户打字不算
    raw_text = _strip_profile_marks(raw_text)
    _note_user_text(raw_text or "")
    # v1.8.10：对方说话了 -> 这条「等回话」作废（话题还在继续）
    try:
        followup_clear(key, "收到对方消息")
        # v1.8.12：收到商城表情先存一份（不阻塞，出错就算了）
        _steal_stickers(event)
        # ★ v1.9.15：这里以前是「关键词抢答」——「画你自己」「换头像」「点歌」只要字面命中，
        #  不看上下文就抢走整轮回复，所以对方发一句「你看这个」都可能被当成要画图，
        #  回一句写死的台词。现在这些动作全部改成模型自己的工具调用（见 CHAT_TOOLS）：
        #  判断由它结合上下文做，动作由系统执行，代码不再用字符串猜意图。
    except Exception:
        pass

    # v1.9.13 修正（QA P3-2）：情绪打分只看用户真说的话，别把「用户连续发送了多条消息…」喂给打分器
    _emo_text = _plain_ask_text(raw_text)
    if not _emo_text:
        if (raw_text or "").startswith(_MEDIA_ONLY_HEAD) or (raw_text or "").startswith(_MERGE_EMPTY[:8]):
            _emo_text = "（对方发了一串表情包，没有文字）"
        else:
            _emo_text = raw_text or ""
    try:
        if _emo is not None:
            _urg = float(_emo.urgency(_emo_text))
    except Exception:
        _urg = 0.0

    # v1.1.3 任务四：情感中枢——判「情绪 / 权重 / 姿态建议」。
    # 权重决定「先回谁」（私聊说失恋 90 比群里哈哈哈 10 优先），
    # 建议会拼进 prompt 当日语气的短指令；判不出来就静默用本地词表，绝不影响回复。
    _emo_dec = None
    try:
        if bool(_live_cfg("emotion_engine_enabled", True)):
            import emotion_engine as _ee
            # v1.9.11：情感判定用「固定的极简 system」+ 不带时间戳，
            # 这样每次调用的前缀字节完全一样，缓存能命中；
            # 人设那份 system 留给正式回复用，不塞给打分器。
            _emo_dec = _ee.analyze(
                _emo_text,
                _live_cfg("emotion_presets", "normal"),
                timeout=float(_live_cfg("emotion_engine_timeout", 0.9) or 0.9),
                ask=lambda _p, **_kw: ask_model_raw(
                    _p, system=EMO_JUDGE_SYSTEM, no_time=True, **_kw))
            _ee.log_decision(_emo_text, _emo_dec, action="准备回复",
                             scope=str(key or "")[:24])
            # v1.1.3 阶段一：每次决策都要进控制台日志（用户点名要能看见拦截过程）
            # v1.8.12（任务一）：记下这个会话最后一次的情绪判定，
            # 情感触发要用它判断「是不是该提前开口」。
            try:
                _le = dict(_emo_dec or {})
                _le["ts"] = time.time()
                _LAST_EMO[str(key or "")] = _le
                if len(_LAST_EMO) > 300:
                    _LAST_EMO.clear()
            except Exception:
                pass
            log(f"[情感] {sender_name}「{(_emo_text or '')[:30]}」→ "
                f"{_emo_dec.get('emotion')} 权重 {_emo_dec.get('weight')} "
                f"（{'模型' if _emo_dec.get('src') == 'model' else '本地'}）"
                + (f"｜建议：{str(_emo_dec.get('suggestion'))[:40]}"
                   if _emo_dec.get("suggestion") else ""), "DEBUG")
            _w = int(_emo_dec.get("weight") or 0)
            if _w >= 0:
                _urg = max(_urg, _w / 100.0)
    except Exception as e:
        log(f"  [情感] 判断失败（忽略）: {e}", "DEBUG")

    # v1.1.2 安全：临时会话（通过群聊发起的一次性会话）默认不回复。
    # NapCat 里这类消息是 private + sub_type=group/other，还有一种直接给 temp_session 标记。
    if mtype == "private" and bool(_live_cfg("ignore_temp_session", True)):
        try:
            if _is_temp_session(event):
                log(f"  [临时会话] 已按设置忽略：{sender_name}", "DEBUG")
                mark_replied(event.get("message_id"))
                return
        except Exception:
            pass

    # v1.1.2 任务三：极简规则拦截（纯图片/「嗯哦哈哈」这类）——不调模型、不写库
    try:
        # v1.8.5：先记一笔「这个会话刚刚在聊」，再判断能不能用快路由。
        # 连着聊的时候 fast_route 会直接返回 None（交给模型，别敷衍人家）。
        _fast_talk_note(key)
        _fast = fast_route(raw_text, has_image=("[CQ:image" in (raw_text or "")), key=key)
        if _fast:
            # 命中也要让模型看一眼：合适就原样发，敷衍就按上下文重写
            _fast = _fast_review(_fast, key, raw_text)
        if _fast:
            human_delay(_fast, _urg)
            _q = quote_target(key, event)
            if target[0] == "private":
                send_private_human(target[1], _fast, reply_to=_q)
            else:
                send_group_human(target[1], _fast, at_user=user_id, reply_to=_q)
            mark_replied(event.get("message_id"))
            log(f"  [快路由] 直接回「{_fast}」，没调模型")
            return
    except Exception as _e:
        log(f"  [快路由] 异常，按正常流程走：{_e}", "DEBUG")

    # 「忘了吧」指令
    if raw_text and re.search(r"(忘了吧|忘掉吧|忘记吧|失忆吧|忘了我)", raw_text):
        n = mem.clear_scope(key)
        reply = random.choice([
            f"好，{sender_name}，刚才那些都随海风飘走啦，鲸鲸失忆完毕。",
            "唔……鲸脑格式化完成。我们从新朋友开始。",
            f"忘了忘了，共 {n} 条记忆，全部倒进海里了。重新认识一下？",
        ])
        human_delay(reply)
        quote_id = quote_target(key, event)   # v1.1.1：隔了几条才引用，太近就不引用
        if target[0] == "private":
            send_private_human(target[1], reply, reply_to=quote_id)
        else:
            send_group_human(target[1], reply, at_user=user_id, reply_to=quote_id)
        mark_replied(event.get("message_id"))
        log(f"  [记忆清空 {n} 条] -> {reply[:60]}")
        return

    # 管理员暗号：命中就立刻在客户端弹 10 秒强提醒。
    # v1.1.1：如果开了「最高权限」，命中后不再回一句固定话术，而是交给模型，
    # 用管理员自己写的提示词专门回（这是「暗号远程对话」的落地方式）。
    _admin_mode = False
    if CODEWORD_ENABLED and raw_text:
        _scope_ok = (CODEWORD_SCOPE == "all"
                     or (CODEWORD_SCOPE == "private" and mtype == "private")
                     or (CODEWORD_SCOPE == "group" and mtype == "group"))
        if _scope_ok:
            _cw = check_codeword(raw_text)
            if _cw.get("matched"):
                raise_codeword_alert(user_id=user_id, name=sender_name, mtype=mtype, key=key)
                # v1.1.1：开了最高权限就走模型（管理员提示词），否则维持原样：
                # 只弹提醒 + 回一句固定话术，不把暗号喂给模型。
                try:
                    import admin as _admin_mod
                    if _admin_mod.bypass_guard() and _admin_mod.admin_prompt_block():
                        _admin_mode = True
                except Exception as _e:
                    log(f"  [暗号] 管理员模块不可用：{_e}", "WARN")
                if _admin_mode:
                    log("  [暗号] 命中 → 管理员模式（用专用提示词回复）")
                    note_recent_msg(key, event.get("message_id"))
                else:
                    reply = random.choice([
                        "暗号收到 ✅ 我知道是你了。",
                        "✅ 暗号确认，本人上线。",
                        "收到收到，暗号对上了 ✅",
                    ])
                    human_delay(reply, _urg)
                    quote_id = quote_target(key, event)   # v1.1.1：隔了几条才引用，太近就不引用
                    if target[0] == "private":
                        send_private_human(target[1], reply, reply_to=quote_id)
                    else:
                        send_group_human(target[1], reply, at_user=user_id, reply_to=quote_id)
                    mark_replied(event.get("message_id"))
                    log(f"  [暗号] 已确认，回复：{reply}")
                    return

    # ── v1.1.0：注入防护（SQL / 提示词 / 命令）──
    # 硬攻击（risk 2）整条拦下不送模型；提示词注入（risk 1）只中和命中的片段再放行，
    # 否则「你删掉刚才的记忆吧」这种正常话会被误杀。
    # v1.1.1：管理员模式（暗号命中 + 开了最高权限）跳过这一步。
    if _admin_mode:
        log("  [防护] 管理员模式：本条不拦")
        _gd = {"blocked": False, "risk": 0, "cleaned": raw_text}
    else:
        _gd = apply_guards(raw_text)
    if _gd["blocked"]:
        reply = random.choice([
            "这句我不接哦，换个说法？",
            "唔…这句话我看不懂，我们聊点别的吧。",
            "这个话头有点怪，我跳过啦。",
        ])
        human_delay(reply, _urg)
        quote_id = quote_target(key, event)   # v1.1.1：隔了几条才引用，太近就不引用
        if target[0] == "private":
            send_private_human(target[1], reply, reply_to=quote_id)
        else:
            send_group_human(target[1], reply, at_user=user_id, reply_to=quote_id)
        mark_replied(event.get("message_id"))
        log(f"  [防护] 拦截了一条 risk={_gd['risk']} 的消息，未送模型")
        return
    if _gd["cleaned"] != raw_text:
        log(f"  [防护] 已中和提示词注入片段（risk={_gd['risk']}）")
        raw_text = _gd["cleaned"]

    # v1.1.1：同一句短话连发（刷屏、复读、测试）只回第一条，
    # 否则就是截图里那种「对方发三条，机器人回三大段」。
    if raw_text and already_replied_same(key, raw_text):
        # v1.8.6（QA P2-2）：以前这里直接 return，用户连发两次同一句，
        # 第二次就石沉大海 —— 用户会以为断网了。现在给一个很轻的反应
        # （「？」「…」这类），同一会话 90 秒内最多一次，不至于反过来刷屏。
        _react = dup_reaction(key)
        if _react:
            try:
                if target[0] == "private":
                    send_private_human(target[1], _react)
                else:
                    send_group_human(target[1], _react, at_user=user_id)
                log(f"  [去重] 同一句又来了，回一个「{_react}」表示在听")
            except Exception as _e:
                log(f"  [去重] 反应没发出去（不影响其它）：{_e}", "DEBUG")
        else:
            log(f"  [去重] 这句刚回过，跳过：{raw_text[:24]}")
        mark_replied(event.get("message_id"))
        return

    images = collect_images(event) if ENABLE_VISION else []
    sticker_hints = extract_sticker_hints(event)
    if images:
        log(f"  附带 {len(images)} 张图片")
    if sticker_hints:
        log(f"  表情包: {sticker_hints}")

    # 从 SQLite 取最近对话作为上下文
    # v1.1.4 阶段二：先做一次「老消息压缩」——超过 N 条的部分压成一句摘要，
    # 这样长对话不会把上下文塞满（最近几条仍原样保留）。
    try:
        compress_old_context(key)
    except Exception as e:
        log(f"[上下文] 压缩失败（不影响回复）：{e}", "DEBUG")
    hist = mem.get_recent_messages(key, turns=_hist_turns())

    # ★ v4 修复 @ 上下文漏洞：
    #   群里只 @"我" 一下、或者只说了很短一句时，把前面紧邻的几条别人说的话
    #   一起拼进 prompt。这样
    #       R: 你吃饭了吗
    #       R: @小鲸鱼
    #   模型能看到"你吃饭了吗"，而不是只看到"（只是@了你一下，没说话）"。
    group_prelude = ""
    if mtype == "group":
        gid = target[1]
        at_msg_id = event.get("message_id")
        # v1.1.3 阶段一：@ 前 N 条 + @ 后 M 条，只要这条是 @ 就拼，不再看字数。
        # （以前「正文超过 6 个字就不拼」，用户实测「@ 后的上下文完全没带上」）
        try:
            _buf = int(_live_cfg("context_buffer_size", GROUP_CONTEXT_LOOKBACK) or 20)
        except Exception:
            _buf = 20
        try:
            n_before = int(_live_cfg("context_before_at", max(1, _buf // 2)) or max(1, _buf // 2))
        except Exception:
            n_before = max(1, _buf // 2)
        try:
            n_after = int(_live_cfg("context_after_at", max(1, _buf // 4)) or max(1, _buf // 4))
        except Exception:
            n_after = max(1, _buf // 4)
        prev = group_ctx.pending_before_at(gid, at_msg_id, limit=n_before)
        after = group_ctx.recent_after(gid, at_msg_id, limit=n_after)
        if prev or after:
            parts = []
            if prev:
                parts.append("【这个群刚才的对话】\n" +
                             "\n".join(f"{m['name']}：{m['text']}" for m in prev))
            if after:
                parts.append("【紧接着又有人说】\n" +
                             "\n".join(f"{m['name']}：{m['text']}" for m in after))
            if not raw_text:
                parts.append("【现在】对方@了你（没说别的）")
            elif len(raw_text) <= 20:
                parts.append(f"【现在】对方@了你：{raw_text}")
            group_prelude = "\n".join(parts) + "\n"
            log(f"  [群上下文] @ 前 {len(prev)} 条 + @ 后 {len(after)} 条"
                f"（缓冲 {_buf}，上限 {n_before}/{n_after}）", "DEBUG")

    sticker_note = ""
    if sticker_hints:
        sticker_note = f"\n（对方发的是表情包：{'、'.join(sticker_hints)}）"

    reuse_images = False
    if not images and ENABLE_VISION and hist:
        # v1.9.13 修正（QA P1）：以前拿合并后的整段去判，合并壳里的「图片/上面」永远命中，
        # 于是「本会话早先发过一张图」之后，任何一次连发都会被当成在问那张图。
        _ask_txt = _plain_ask_text(raw_text)
        asking_about_image = bool(_ask_txt) and any(
            kw in _ask_txt for kw in
            ("图", "上面", "这谁", "这是谁", "写的什么", "写了什么", "什么图", "这什么", "这张"))
        if asking_about_image:
            last_imgs = sessions.get_images(key)
            if last_imgs:
                images = last_imgs
                reuse_images = True
                log(f"  追问带图：复用上一轮 {len(images)} 张图片")

    prompt = f"{sender_name}：{raw_text}" if raw_text else f"{sender_name}：发了一张图"
    if images and not raw_text:
        prompt = f"{sender_name}：发了一张图片，请看看是什么，用你小鲸鱼的口吻自然回应。"
    elif images and reuse_images:
        prompt = (f"{sender_name}：{raw_text}\n"
                  f"（这条消息指的是你上面看到的那张图片，请基于图片内容直接回答，不要说不清楚）")
    elif images:
        prompt = f"{sender_name}：{raw_text}\n（消息里附带了图片，请结合图片内容回应）"
    elif sticker_hints and not raw_text:
        prompt = f"{sender_name}：发了个表情包{sticker_note}，请自然地回应一下。"

    # v1.1.3 任务四：把情感中枢给出的「语气建议」塞进 prompt（短一句，不喧宾夺主）
    # v1.1.5.1 补：光有「建议」太笼统，改成「情绪 + 强度 + 具体回法 + 人设语气」，
    # 由 emotion_engine.plan_hint 折成一句可执行的指令（用户反馈判得太笼统）。
    try:
        if _emo_dec:
            _hint = ""
            try:
                import emotion_engine as _ee2
                _hint = _ee2.plan_hint(
                    _emo_dec, _live_cfg("emotion_presets", "normal"))
            except Exception:
                _hint = str(_emo_dec.get("suggestion") or "")
            if _hint:
                prompt += f"\n（对方这句的情绪与回法：{_hint}）"
    except Exception:
        pass

    if group_prelude:
        prompt = group_prelude + "\n" + prompt

    # ★ ask_model 现在永不返回 None
    perm_uid = str(user_id) if target[0] == "private" else ""
    _kind_cn = "群聊" if target[0] == "group" else "私聊"
    # v1.1.2 任务一：请求模型这段时间先抛个「正在输入」，发完再取消
    try:
        set_typing(target, True)
    except Exception:
        pass
    _remember_begin()
    reply = ""
    # v1.4.0：这句话像是「让它去干点什么」→ 走动手模式（跑完没结论再退回普通聊天）
    if agent_wanted(raw_text):
        try:
            reply = auto_agent_reply(raw_text, key) or ""
        except Exception as e:
            log(f"[Agent] 自动动手失败，退回普通聊天：{e}", "WARN")
            reply = ""
    if not reply:
        reply = ask_model(hist, prompt, images=images, scope=key,
                          perm_user=perm_uid, perm_name=sender_name,
                          detail=f"{_kind_cn} · {sender_name}")
    # v1.3.2（QA BUG-08）：模型在正文里写的「记住: xxx」已经被 clean_reply 剥掉，
    # 条目攒在本线程的槽里 —— 主线程取走，再交给后台任务落库（线程局部变量跨不了线程）。
    _rmb_items = _remember_take()
    try:
        set_typing(target, False)
    except Exception:
        pass

    if images and not reuse_images:
        sessions.set_images(key, images)

    # V1.9.9.2：图上要留标记（等下异步补一句注记），别只剩「[图片]」三个字
    # v1.9.16（QA P3-1）：落库/写历史一律用 _memory_text()，把「[消息1：…]」这种
    # 只给模型看的合并壳剥掉 —— 否则模型下一轮会从自己的历史里学会这种机器腔。
    _mem_raw = _memory_text(raw_text)
    if images:
        _db_user_text = ("[用户发送了一张图片] " + _mem_raw).strip()
        hist_text = f"{sender_name}：{_db_user_text}"
    else:
        _db_user_text = _mem_raw or "[图片]"
        hist_text = f"{sender_name}：{_mem_raw}" if _mem_raw else f"{sender_name}：[图片]"
    sessions.add(key, "user", hist_text)
    sessions.add(key, "assistant", reply)

    def _after_reply_job():
        """v1.1.6（任务三）：回复相关的落库、提取、摘要全部塞进后台线程。

        这些都不是「用户要看到回复」的必要条件，放在主线程里只会让下一次
        请求排在同一条线程后面，表现出来就是「回完这条要愣两秒才理下一条」。
        每个后台任务自己 new Memory()/自己 commit，绝不复用主线程的连接。
        """
        # V1.9.9.2：先给图补一句注记，再写库 —— 写进去的是「[发送了一张图片：一碗面] 网上买的」
        _u_text = _db_user_text
        if images and not reuse_images and ENABLE_VISION:
            try:
                _note = vision_caption(images[0], _mem_raw or raw_text)
            except Exception:
                _note = ""
            if _note:
                # v1.9.16：注记也拼在「原话」上，别把合并壳一起写进历史
                _u_text = ("[用户发送了一张图片，识别内容为：" + _note + "] "
                           + (_mem_raw or "")).strip()
                try:
                    sessions.patch_last_user(key, f"{sender_name}：{_u_text}")
                    log("  [图片注记] %s" % _note, "DEBUG")
                except Exception as _e_p:
                    log("  [图片注记] 回填历史失败：%s" % _e_p, "DEBUG")
        try:
            if MEMORY_ENABLED:
                mem.add_message(key, "user", _u_text)
                mem.add_message(key, "assistant", reply)
                person = f"{sender_name}({user_id})"
                mem.log_message(key, person, "user", _u_text)
                mem.log_message(key, "小鲸鱼", "assistant", reply)

                # v1.5.0：事后自省 —— 这句把人惹毛了没有？惹了的话过一会儿自己回来
                # v1.9.16：传原话，别让自省看到一个「[消息1：…]」的包装壳
                try:
                    maybe_post_reflect(_mem_raw or raw_text, reply, key, user_id,
                                       sender_name, target)
                except Exception as _e_pr:
                    log(f"[事后自省] 排程失败（忽略）: {_e_pr}", "DEBUG")

                # v1.3.2（QA BUG-08）：模型写在正文里的「记住: xxx」不发给用户了，
                # 但内容不能丢 —— 这里补写进永久记忆（私聊才写，群聊不记别人隐私）。
                if _rmb_items and target[0] == "private":
                    for _it in _rmb_items:
                        try:
                            _txt = _it if _it.startswith("（") else f"（{sender_name}）{_it}"
                            _mid, _new, _ = mem.add_perm(
                                user_id=str(user_id), content=_txt[:200],
                                category="fact", importance=4, source="model-remember")
                            if _new:
                                log(f"[记忆] 模型要求记住：{_txt[:40]}", "INFO")
                        except Exception as e3:
                            log(f"[记忆] 写入「记住:」条目失败（忽略）: {e3}", "DEBUG")

                # v1.1.6 核心任务一：把这句用户原话写进语义库（后台，失败不影响回复）
                # v1.9.21（QA P2-1）：这里以前传的是 raw_text —— 连发合并时 raw_text 是
                # 「用户连续发送了多条消息，请综合这些内容统一回复：[消息1：…]…（提示：…）」
                # 这段内部包装文案，一旦写进 vec_memory 就会被检索出来回喂给模型，模型还可能复读。
                # 1.9.16 修「记忆存原话」时漏了这一处（同段的 extract_and_store / extract_events 也漏了）。
                _mem_only = _mem_raw or raw_text
                try:
                    if bool(_live_cfg("semantic_enabled", True)) and (_mem_only or "").strip():
                        import semantic as _sem_add
                        _sem_add.add("chat", _mem_only, session_key=key, user_id=str(user_id))
                except Exception as e2:
                    log(f"[语义] 写入失败（忽略）: {e2}", "DEBUG")

                if MEMORY_AUTO_SAVE:
                    threading.Thread(target=extract_and_store,
                                     args=(key, sender_name, _mem_only), daemon=True).start()
                    threading.Thread(target=extract_events,
                                     args=(key, sender_name, user_id, _mem_only), daemon=True).start()
                    if target[0] == "private":
                        threading.Thread(target=_perm_extract_job,
                                         args=(key, sender_name, user_id), daemon=True).start()

            if (bool(_live_cfg("affinity_enabled", False)) and target[0] == "private"
                    and (raw_text or "").strip()):
                # v1.9.21：同样只喂用户原话，别把「用户连续发送了多条消息…」的包装壳带进去
                bump_affinity_for(str(user_id), _mem_raw or raw_text, sender_name)

            if MEMORY_ENABLED:
                try:
                    msg_count = mem.count_messages(key)
                    if msg_count >= 32 and msg_count % 32 == 0:
                        early = mem.get_earliest_messages(key, msg_count // 2)
                        if early:
                            dialog = "\n".join(f"{r}: {c_}" for r, c_ in early)
                            # v1.9.23：同上，摘要也跟聊天走同一条链（分工 summary 优先）
                            summary_text, _reason = _extract_text(
                                f"请将以下对话压缩成 100 字以内的中文摘要，保留关键事实和决定：\n{dialog}",
                                SUMMARY_BUDGET, temperature=0.2,
                                tag="/滚动摘要", feature="summary", timeout=120)
                            if summary_text:
                                # v1.9.24：raw 返回不再经过 clean_reply，自己清一下围栏
                                _sm = re.sub(r"```[a-zA-Z]*", "", summary_text).strip()
                                mem.save_summary(key, _sm[:100])
                                log(f"  [滚动摘要] 已写入: {_sm[:60]}")
                except Exception as e:
                    log(f"  [滚动摘要] 异常: {e}", "ERROR")
        except Exception as e:
            log(f"[后台落库] 异常（不影响这条回复）: {e}", "WARN")

    try:
        threading.Thread(target=_after_reply_job, daemon=True, name="after-reply").start()
    except Exception as e:
        log(f"[后台落库] 起线程失败，退回同步: {e}", "WARN")
        _after_reply_job()

    human_delay(reply, _urg)

    # ★ 活人感：引用只用来「指回哪句」，不是每条都引用
    # v1.1.1：紧挨着的一两条直接回，不引用；隔了几条才引用（quote_gap 可调）
    quote_id = quote_target(key, event)

    if target[0] == "private":
        send_private_human(target[1], reply, reply_to=quote_id)
    else:
        send_group_human(target[1], reply, at_user=user_id, reply_to=quote_id)

    # v1.1.2：真的发出去了才登记「这句回过」，避免模型失败也占位
    # v1.9.16：登记也用原话 —— 存了合并壳的话，下次同样的原话就永远对不上了
    note_replied_text(key, _mem_raw or raw_text)

    # v1.4.0：记录模式 —— 把「他说的 → 我回的」记下来，说得多了下次第一遍就命中
    try:
        import route_memo
        _n = route_memo.remember(_mem_raw or raw_text, reply)
        if _n:
            log(f"[记录模式] 「{(_mem_raw or raw_text)[:14]}」记下第 {_n} 次", "DEBUG")
    except Exception as _e:
        log(f"[记录模式] 记录失败（忽略）: {_e}", "DEBUG")

    # ★ 活人感：有概率跟一个表情包（表情包是独立的一条消息，更自然）
    try:
        maybe_send_sticker(key, reply, reply_to=None)
    except Exception as e:
        log(f"[表情包] 后续发送异常（忽略）: {e}", "DEBUG")

    # ★ v1.9.15：这里以前会「按概率自己画一张」+ 一堆关键词正则（画一张 / 看看你…）。
    #   现在画图只走模型自己的工具调用（CHAT_TOOLS: draw_picture / draw_self_portrait），
    #   代码不再用字符串猜意图 —— 对方发个搞笑视频，它不会突然画张图发过去。

    mark_replied(event.get("message_id"))
    log(f"  -> {reply[:90]}")


# ---------------------------------------------------------------
# 说明：这里没有 `import agent`；需要 agent 的活名字一律走 core.globals。
# ---------------------------------------------------------------
