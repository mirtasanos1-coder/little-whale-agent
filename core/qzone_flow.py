# -*- coding: utf-8 -*-
"""空间（说说/动态）与漏回复补发（从 agent.py 物理搬出来，逻辑没改）。

两件事：
  · 看动态并回评论：feed_fetch（抓一页）→ feed_round（决定回不回）→
    feed_comment_round（回复走空间网页协议，三道闸防重复）
  · 漏回复补发：catch_up_unread（群）/ catch_up_private（私聊）

状态仍然记在 agent 的 state / _feed_state 里（重启不丢），
会话池 sessions、NapCat 连接 napcat 也都是 agent 那一份 —— 全走 core.globals。
"""
import json
import os
import time

import qzone as qzone_mod
from personas import active_name

from core import globals as _g
from core.utils import clean_reply, log  # noqa: F401

_SYNC_NAMES = ('_live_cfg', '_CFG', 'DATA_DIR', 'FEED_ENABLED', 'FEED_DECIDE', 'FEED_MAX_PER_ROUND', 'FEED_POLL_MIN', 'FEED_REPLY_PROMPT', 'CATCHUP_ENABLED', 'CATCHUP_GROUPS', 'CATCHUP_MAX_AGE_MIN', 'CATCHUP_MAX_MSGS', 'CATCHUP_MIN_GAP_SEC', 'PRIVATE_CATCHUP_ENABLED', 'PRIVATE_CATCHUP_FIRST_HOURS', 'ENABLE_VISION', 'MAX_IMAGES_PER_MSG', 'MEMORY_ENABLED', 'mem', 'state', 'save_state', 'napcat', '_qi', '_feed_flag', '_feed_state', '_feed_decision', '_skip_self_post', '_cmt_mark_sent', '_cmt_sent_recent', '_comment_at_text', '_hist_turns', '_img_to_data_url', 'download_image', 'mark_replied', 'msg_plain', 'replied_ids', '_find_pending_private', '_get_friend_list', '_get_recent_private_peers', '_private_msg_text', 'ask_model', 'ask_model_raw', 'send_group_human', 'send_private_human', 'clean_reply', 'PQ_PRIO_BG')


def _sync():
    """把 agent 里那些活名字的最新值灌进本模块。"""
    _g.sync(_SYNC_NAMES, globals())


# ------------------------------------------------------------------
# 搬自 agent.py L5039-5169：catch_up_unread
# ------------------------------------------------------------------
def catch_up_unread():
    # v1.1.6 ⑧ 上线补回开关：以前是硬编码 True + 写死群号，现在走设置项（群聊/私聊各自可关）。
    _sync()
    if not bool(_live_cfg("group_catchup_enabled", CATCHUP_ENABLED)):
        log("离线补回：群聊补回已在设置里关闭，跳过")
        return
    if not CATCHUP_GROUPS:
        log("离线补回：未配置群号，跳过")
        return

    me = None
    info = napcat("get_login_info")
    if info and info.get("status") == "ok":
        me = str(info["data"]["user_id"])
    if not me:
        log("离线补回：拿不到自己的账号，跳过", "WARN")
        return

    cutoff = time.time() - CATCHUP_MAX_AGE_MIN * 60
    last_ts = state["last_catchup_ts"]
    if last_ts > 0:
        cutoff = max(cutoff, last_ts)

    log(f"离线补回：检查最近 {CATCHUP_MAX_AGE_MIN} 分钟内的 @ 消息")

    any_aborted = False
    for gid in CATCHUP_GROUPS:
        try:
            r = napcat("get_group_msg_history", group_id=gid, count=40)
            if not r or r.get("status") != "ok":
                continue
            msgs = (r.get("data") or {}).get("messages") or []
            pending = []
            for m in msgs:
                if m.get("sender", {}).get("user_id") == int(me):
                    continue
                mid = int(m.get("message_id", 0))
                if mid in replied_ids:
                    continue
                ts = m.get("time", 0)
                if ts <= last_ts or ts < cutoff:
                    continue
                plain = msg_plain(m.get("message"))
                if f"@{me}" not in plain:
                    continue
                pending.append(m)

            if not pending:
                log(f"  群 {gid}: 没有待补的 @ 消息")
                continue

            _cmax = int(_live_cfg("group_catchup_max_msgs", CATCHUP_MAX_MSGS) or CATCHUP_MAX_MSGS)
            pending = pending[-max(1, _cmax):]
            log(f"  群 {gid}: 发现 {len(pending)} 条未回复的 @ 消息，开始补回")
            aborted = False

            # v1.1.2：把这一批漏掉的 @ 合并成「一次」回复，并且带上真实历史。
            # 老实现是逐条 ask_model([], prompt) —— 没有历史、没有前情，
            # 模型只能干巴巴回「怎么了」「在的」，用户直接说「上下文拼接不好」。
            lines = []
            imgs = []
            last_mid = None
            last_uid = None
            for m in pending:
                uid = m.get("sender", {}).get("user_id")
                name = (m.get("sender", {}).get("card")
                        or m.get("sender", {}).get("nickname") or str(uid))
                text = msg_plain(m.get("message")).replace(f"@{me}", "").strip()
                if not text:
                    text = "（只是@了你一下，没说话）"
                mins = int((time.time() - int(m.get("time") or 0)) / 60)
                ago = "刚刚" if mins <= 1 else (f"{mins} 分钟前" if mins < 60
                                                else f"{mins // 60} 小时前")
                lines.append(f"{name}（{ago}）：{text}")
                last_mid = m.get("message_id") or last_mid
                last_uid = uid or last_uid
                if ENABLE_VISION and len(imgs) < MAX_IMAGES_PER_MSG:
                    for seg in (m.get("message") or []):
                        if seg.get("type") == "image":
                            u = seg.get("data", {}).get("url")
                            if u and u.startswith("http"):
                                data, mime = download_image(u)
                                if data:
                                    imgs.append(_img_to_data_url(data, mime))
                            if len(imgs) >= MAX_IMAGES_PER_MSG:
                                break

            _hist = []
            if MEMORY_ENABLED:
                try:
                    _hist = mem.get_recent_messages(f"group_{gid}", turns=_hist_turns())
                except Exception:
                    _hist = []
            prompt = ("【你离线/没上线期间，群里这些消息是在叫你的】\n"
                      + "\n".join(lines)
                      + "\n【现在】接着当时的对话自然回一句（只要一条，别逐条回答，"
                        "别写小作文），不要提「刚看到」「不好意思现在才回」这种话。")
            reply = ask_model(_hist, prompt, images=imgs or None, scope=f"group_{gid}",
                              allow_fallback=False, detail="群聊补回")
            if not reply:
                log("  [群补回] 模型不可用，本轮中止（不推进时间戳，下次再试）", "WARN")
                aborted = True
                break
            send_group_human(gid, reply, at_user=last_uid, reply_to=None)
            if last_mid:
                mark_replied(last_mid)
            if MEMORY_ENABLED:
                try:
                    mem.add_message(f"group_{gid}", "user", " / ".join(lines)[:1000])
                    mem.add_message(f"group_{gid}", "assistant", reply)
                except Exception:
                    pass
            if ts > state["last_catchup_ts"]:
                state["last_catchup_ts"] = ts
                save_state()
            log(f"    补回（{len(lines)} 条合并）-> {reply[:60]}")
            time.sleep(CATCHUP_MIN_GAP_SEC)

            if aborted:
                any_aborted = True
                continue

        except Exception as e:
            log(f"  群 {gid} 补回失败: {e}", "ERROR")

    # 有群因为模型不可用而中止时，不推进全局时间戳 ——
    # 否则这批 @ 消息会被 ts <= last_catchup_ts 永久跳过，再也补不回来。
    if any_aborted:
        log("[群补回] 本轮有群中止，不推进时间戳（下次重试）", "WARN")
    else:
        state["last_catchup_ts"] = int(time.time())
        save_state()


# ------------------------------------------------------------------
# 搬自 agent.py L5280-5413：catch_up_private
# ------------------------------------------------------------------
def catch_up_private(dry_run=False):
    """补回所有未回的私聊消息（不限时间，受条数限制）。

    在启动时跑一次，之后由 care_loop 每隔一段时间再跑。
    """
    _sync()
    if not bool(_live_cfg("private_catchup_enabled", PRIVATE_CATCHUP_ENABLED)):
        log("[私聊补回] 已禁用")
        return

    info = napcat("get_login_info")
    me = None
    if info and info.get("status") == "ok":
        me = int(info["data"]["user_id"])
    if not me:
        log("[私聊补回] 拿不到自己的账号，跳过", "WARN")
        return

    friends = _get_friend_list()
    peers = _get_recent_private_peers()
    if not peers:
        log("[私聊补回] 最近没有私聊联系人")
        return

    log(f"[私聊补回] 扫描 {len(peers)} 个最近联系人")
    cursor = state.setdefault("private_cursor", {})
    total_sent = 0

    for uid, _last_ts in peers:
        if uid == me:
            continue
        name = friends.get(uid) or str(uid)
        try:
            r = napcat("get_friend_msg_history", user_id=uid, count=20)
            if not r or r.get("status") != "ok":
                continue
            msgs = (r.get("data") or {}).get("messages") or []
            if not msgs:
                continue

            # 上一次处理到的时间点；第一次见这个好友就只看最近 N 小时
            last_done = int(cursor.get(str(uid), 0))
            if last_done <= 0:
                last_done = int(time.time() - PRIVATE_CATCHUP_FIRST_HOURS * 3600)

            pending = _find_pending_private(msgs, me, last_done)
            if not pending:
                continue
            log(f"  [私聊补回] {name}({uid}): 发现 {len(pending)} 条未回消息")
            aborted = False

            if dry_run:
                for m in pending:
                    log(f"    [dry-run] 将补回 id={m.get('message_id')} "
                        f"t={m.get('time')} :: {_private_msg_text(m)[:60]}")
                continue

            for m in pending:
                mid = m.get("message_id")
                text = _private_msg_text(m)
                age_min = int((time.time() - int(m.get("time") or 0)) / 60)
                if age_min > 60 * 24:
                    ago = f"{age_min // 1440} 天前"
                elif age_min > 60:
                    ago = f"{age_min // 60} 小时前"
                else:
                    ago = f"{age_min} 分钟前"
                if not text:
                    text = "（发了个表情/图片）"

                # 带图消息：尽量把图片也读进来
                images = []
                if ENABLE_VISION:
                    for seg in (m.get("message") or []):
                        if seg.get("type") in ("image", "mface"):
                            u = seg.get("data", {}).get("url")
                            if u and u.startswith("http"):
                                data, mime = download_image(u)
                                if data:
                                    images.append(_img_to_data_url(data, mime))
                            if len(images) >= MAX_IMAGES_PER_MSG:
                                break

                # v1.1.2：带上真实历史再补回，否则模型看不到前面聊了啥，只会回「在的」
                _pm_hist = []
                if MEMORY_ENABLED:
                    try:
                        _pm_hist = mem.get_recent_messages(f"private_{uid}",
                                                           turns=_hist_turns())
                    except Exception:
                        _pm_hist = []
                prompt = (
                    f"{name}（{ago}发的，你当时没看到）：{text}\n"
                    f"（这是你漏掉的私聊消息，现在补回。自然地接上这个话题，"
                    f"不要提「刚看到」「不好意思现在才回」这类话，就当刚聊到这儿。）"
                )
                # allow_fallback=False：模型挂了宁可这一轮不补，也不要给真人发"我脑子卡壳了"
                reply = ask_model(_pm_hist, prompt, images=images, scope=f"private_{uid}",
                                  allow_fallback=False, detail="私聊补回")
                if not reply:
                    log(f"  [私聊补回] {name}: 模型不可用，本轮中止（游标不推进，下次再试）", "WARN")
                    aborted = True
                    break
                send_private_human(uid, reply, reply_to=mid)
                mark_replied(mid)
                # 记进会话历史，后续对话能接上
                if MEMORY_ENABLED:
                    try:
                        mem.add_message(f"private_{uid}", "user", text)
                        mem.add_message(f"private_{uid}", "assistant", reply)
                    except Exception:
                        pass
                total_sent += 1
                log(f"    补回 -> {name}: {reply[:60]}")
                time.sleep(CATCHUP_MIN_GAP_SEC)

            # 模型不可用导致中止时，游标不推进 —— 否则这几条永远补不回来了
            if aborted:
                continue

            # 游标推进到"这条会话里最后一条消息的时间"，而不是当前时间。
            # 用当前时间会把补回期间对方新发的消息一起吃掉。
            last_seen = max(int(m.get("time") or 0) for m in msgs) or int(time.time())
            cursor[str(uid)] = last_seen
            save_state()

        except Exception as e:
            log(f"  [私聊补回] {name}({uid}) 失败: {type(e).__name__}: {e}", "ERROR")

    if dry_run:
        log("[私聊补回] dry-run 结束（未实际发送）")
    elif total_sent:
        log(f"[私聊补回] 本轮共补回 {total_sent} 条")
    else:
        log("[私聊补回] 没有需要补回的私聊")


# ------------------------------------------------------------------
# 搬自 agent.py L6159-6244：feed_fetch
# ------------------------------------------------------------------
def feed_fetch():
    """取"别人的动态"。

    QQ 空间的"好友动态列表"官方 NapCat 一直没有接口（实测 10 个动作全是
    「不支持的Api」），所以走两条路：
      ① 新版 NapCat 如果支持 get_qzone_msg_list，就直接用
      ② 否则读 feed_inbox/ 目录里人放的 JSON（开发者/别的工具写进去）
    """
    _sync()
    out = []
    # v1.1.2 #8：先走空间网页协议（能读到别人动态），NapCat 只兜底
    qi = _qi()
    if qi is not None:
        try:
            r = qi.read_friend_feeds(max_friends=6, num=6)
            if r and r.get("ok") and r.get("feeds"):
                items = []
                for f in r["feeds"]:
                    tid = str(f.get("tid") or "")
                    if not tid:
                        continue
                    if _skip_self_post(f.get("uin") or f.get("author_id"),
                                       f.get("name") or f.get("author_name")):
                        log("[动态] 这条是自己发的，跳过点赞/评论", "DEBUG")
                        continue
                    items.append({
                        "post_key": tid,
                        "author_id": str(f.get("uin") or ""),
                        "author_name": str(f.get("name") or ""),
                        "content": str(f.get("content") or ""),
                        "images": "|".join(str(x) for x in (f.get("images") or [])),
                        "source": "qzone",
                    })
                if items:
                    return items
        except Exception as e:
            log(f"[动态] 空间网页读取失败（退回 NapCat）：{e}", "DEBUG")
    try:
        cap = qzone_mod.caps()
    except Exception:
        cap = {}
    if cap.get("read"):
        try:
            ok, items, _msg = qzone_mod.read_feed()
            if ok and items:
                items = [x for x in items
                         if not _skip_self_post(x.get("author_id"), x.get("author_name"))]
                if items:
                    return items
        except Exception as e:
            log(f"[动态] 接口读取失败（改读收件箱）：{e}", "DEBUG")

    # 收件箱兜底
    d = os.path.join(DATA_DIR, str(_CFG.get("feed_dir") or "feed_inbox"))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return out
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log(f"[动态] 读不懂 {name}：{e}", "WARN")
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            content = str(it.get("content") or it.get("text") or "").strip()
            if not content:
                continue
            if _skip_self_post(it.get("uin") or it.get("author_id"),
                               it.get("name") or it.get("author_name")):
                continue
            out.append({
                "post_key": str(it.get("tid") or it.get("post_key") or f"inbox:{name}:{content[:20]}"),
                "author_id": str(it.get("uin") or it.get("author_id") or ""),
                "author_name": str(it.get("name") or it.get("author_name") or "一个好友"),
                "content": content,
                "images": it.get("images") or "",
                "source": "inbox",
            })
    return out


# ------------------------------------------------------------------
# 搬自 agent.py L6278-6398：feed_round
# ------------------------------------------------------------------
def feed_round(force=False):
    """刷一轮动态。返回 (处理了几条, 说明)。"""
    _sync()
    if not FEED_ENABLED and not force:
        return 0, "未启用"
    if not mem:
        return 0, "记忆模块未就绪"
    st = _feed_state()
    now = time.time()
    if not force:
        # v1.1.7 修（P0-2）：这里以前写死 max(60, FEED_POLL_MIN*60)，
        # 把 config.py:768 允许的 1~1440 分钟全夹成「至少一小时」。
        # 现在跟着设置走，下限 1 分钟，避免用户设 5 分钟却一小时才刷一次。
        gap = max(60, int(_live_cfg("feed_poll_min", FEED_POLL_MIN) or FEED_POLL_MIN) * 60)
        if now - float(st.get("last_at") or 0) < gap:
            return 0, "还没到下一轮"


    posts = feed_fetch()
    if not posts:
        st["last_at"] = now
        return 0, "没刷到新动态（NapCat 暂不支持读好友动态，可把内容放进 feed_inbox/）"

    done = 0
    for p in posts:
        if done >= max(1, int(FEED_MAX_PER_ROUND)):
            break
        key = p.get("post_key") or ""
        try:
            if key and mem.feed_has(key):
                continue
        except Exception:
            pass
        like, reply = False, ""
        if FEED_DECIDE == "always":
            like, reply = True, "这个不错" if not p.get("images") else "图挺好看的"
        elif FEED_DECIDE == "model":
            like, reply = _feed_decision(p)
        else:  # readonly
            like, reply = False, ""
        # v1.1.2 #8：点赞 / 评论 拆成两个开关，改完立刻生效；只读模式一律不动手
        if FEED_DECIDE == "readonly":
            like, reply = False, ""
        else:
            if like and not _feed_flag("feed_like_enabled", True):
                like = False
            if reply and not _feed_flag("feed_comment_enabled", True):
                reply = ""

        action = "看到"
        if like:
            action = "点赞"
        if reply:
            action = "评论"
        try:
            mem.feed_mark(key, source=p.get("source", ""),
                          author_id=p.get("author_id", ""),
                          author_name=p.get("author_name", ""),
                          content=p.get("content", ""),
                          images=str(p.get("images") or ""),
                          action=action,
                          reply=reply,
                          status=("replied" if reply else ("liked" if like else "seen")))
        except Exception as e:
            log(f"[动态] 落库失败：{e}", "WARN")
        done += 1
        log(f"[动态] {p.get('author_name') or '好友'}：{(p.get('content') or '')[:40]} "
            f"→ {action}" + (f"「{reply}」" if reply else ""))
        # v1.1.2 #8：能动手就真动手（空间网页协议优先，NapCat 只兜底）
        _qi_m = _qi()
        if like:
            _liked = False
            if _qi_m is not None:
                try:
                    _lr = _qi_m.like(p.get("post_key"), p.get("author_id") or "")
                    _liked = bool(_lr.get("ok"))
                    if _liked:
                        log(f"[动态] 已点赞：{p.get('author_name') or '好友'}")
                    else:
                        log(f"[动态] 点赞失败：{_lr.get('msg') or '未知原因'}", "WARN")
                except Exception as e:
                    log(f"[动态] 点赞异常：{e}", "WARN")
            if not _liked:
                try:
                    if (qzone_mod.caps() or {}).get("like"):
                        _ok, _msg = qzone_mod.like_feed(p.get("post_key"))
                        _liked = bool(_ok)
                        if not _ok:
                            log(f"[动态] 点赞失败（NapCat）：{_msg}", "WARN")
                    else:
                        log("[动态] 没有可用的点赞通道，跳过（只记录）", "DEBUG")
                except Exception as e:
                    log(f"[动态] 点赞异常（NapCat）：{e}", "WARN")
        if reply and p.get("post_key"):
            _said = False
            _said_cid = ""
            if _qi_m is not None:
                try:
                    _cr = _qi_m.comment(p.get("post_key"), p.get("author_id") or "", reply)
                    _said = bool(_cr.get("ok"))
                    _said_cid = str(_cr.get("comment_id") or "")
                    if _said:
                        log(f"[动态] 评论已发出：{reply[:40]}")
                    else:
                        log(f"[动态] 评论发送失败：{_cr.get('msg') or '未知原因'}", "WARN")
                except Exception as e:
                    log(f"[动态] 评论异常：{e}", "WARN")
            # v1.9.20：不再有「NapCat 兜底」那一段 —— qzone.post_comment() 现在也是网页协议，
            # 重试同一个请求只会在第一次其实成功、只是没解析出结果时多发一条。
            if _said:
                try:
                    mem.add_our_comment(reply, moment_tid=p.get("post_key"),
                                        parent_id=0, root_id=0, depth=0,
                                        remote_id=_said_cid)
                except Exception as e:
                    log(f"[动态] 记下我们自己的评论失败：{e}", "DEBUG")
            elif _qi_m is None:
                log("[动态] 没有可用的评论通道，跳过（只记录）", "DEBUG")

    st["last_at"] = time.time()
    save_state()
    return done, f"本轮处理 {done} 条"


# ------------------------------------------------------------------
# 搬自 agent.py L6415-6550：feed_comment_round
# ------------------------------------------------------------------
def feed_comment_round(force=False):
    """v1.1.2 #8：回「别人评论了我的说说」。返回 (回了几条, 说明)。

    走空间网页协议：读自己的说说 → 拉评论 → 跳过自己的 → 让模型回一条 → 发出去。
    """
    _sync()
    qi = _qi()
    if qi is None:
        return 0, "qzone_interact 不可用（没法读评论）"
    if not force and not _feed_flag("feed_reply_enabled", True):
        return 0, "回评论已关闭"
    if not mem:
        return 0, "记忆模块未就绪"
    st = _feed_state()
    now = time.time()
    try:
        gap_min = max(1, int(_live_cfg("feed_reply_check_min", 10) or 10))
    except Exception:
        gap_min = 10
    if not force and now - float(st.get("reply_at") or 0) < gap_min * 60:
        return 0, f"还没到下一轮（每 {gap_min} 分钟看一次）"

    replied = [str(x) for x in (st.get("replied") or [])]
    # v1.9.22：评论回复统一由「评论循环」（moment_comment_loop）负责 —— 它读的是
    # 同一批网页评论，有 DB 状态 + 5 分钟缓存 + 无条件标记三道闸。这里要是也发，
    # 同一条评论就会被两个循环各回一遍。评论循环开着时，这里只入库、不发送。
    _own_by_comment_loop = bool(_live_cfg("moment_comment_enabled", False))
    try:
        me_uin = str((qi.session() or {}).get("uin") or "")
    except Exception:
        me_uin = ""
    done = 0
    try:
        feeds = (qi.list_feeds("", num=5) or {}).get("feeds") or []
    except Exception as e:
        st["reply_at"] = now
        save_state()
        return 0, f"读自己的说说失败：{e}"
    for f in feeds:
        tid = str(f.get("tid") or "")
        if not tid:
            continue
        try:
            if int(f.get("cmtnum") or 0) <= 0:
                continue
        except Exception:
            continue
        try:
            cl = qi.comment_list(tid, "")
        except Exception as e:
            log(f"[动态] 读评论失败：{e}", "DEBUG")
            continue
        for c in (cl or {}).get("comments") or []:
            cid = str(c.get("comment_id") or "")
            cuin = str(c.get("uin") or "")
            if not cid or cid in replied:
                continue
            if me_uin and cuin == me_uin:
                continue
            who = str(c.get("name") or cuin or "有人")
            text = str(c.get("content") or "").strip()
            if not text:
                continue
            # v1.9.22：我们自己发的回复会被接口读回来（内容是「回复 @某某 ：…」），
            # 拿不到自己 uin 的时候必须靠这个特征认出来，不然就成了自己回自己。
            if text.startswith("回复 @") and (not cuin or cuin == me_uin):
                replied.append(cid)
                continue
            if _own_by_comment_loop:
                # 只灌进库，交给评论循环去回（它一次只会回一次）
                try:
                    mem.add_comment(text, commenter_id=cuin, commenter_name=who,
                                    moment_tid=tid,
                                    moment_text=str(f.get("content") or "")[:1000],
                                    source="qzone_web", remote_id=cid)
                except Exception as e:
                    log(f"[动态] 评论入库失败：{e}", "DEBUG")
                replied.append(cid)
                continue
            try:
                prompt = FEED_REPLY_PROMPT.format(
                    name=active_name() or "小鲸鱼", who=who, text=text[:200],
                    post=str(f.get("content") or "")[:120])
                r = ask_model_raw(prompt, max_tokens=200, effort="none", tag="/回评论",
                                  prio=PQ_PRIO_BG)
                reply = clean_reply(r or "")
            except Exception as e:
                log(f"[动态] 生成回评论失败：{e}", "WARN")
                continue
            if not reply:
                continue
            reply = reply[:80]
            # v1.9.20：同样要带「回复 @昵称 ：」，并且过 5 分钟去重
            _pc = {"moment_tid": tid, "commenter_id": cuin,
                   "commenter_name": who, "content": text}
            if _cmt_sent_recent(_pc):
                replied.append(cid)
                continue
            _body = _comment_at_text(who, reply)
            ok = False
            _get_cid = ""
            try:
                _ok, _msg = qzone_mod.comment_web(tid, _body, reply_comment_id=cid)
                ok = bool(_ok)
                _get_cid = str(getattr(qzone_mod, "LAST_COMMENT_ID", "") or "")
                _cmt_mark_sent(_pc)
                if not ok:
                    log(f"[动态] 回评论失败：{_msg or '未知原因'}", "WARN")
            except Exception as e:
                log(f"[动态] 回评论异常：{e}", "WARN")
            if ok:
                replied.append(cid)
                done += 1
                log(f"[动态] 回了 {who} 的评论：{_body[:40]}")
                try:
                    mem.add_our_comment(_body, moment_tid=tid, depth=1, remote_id=_get_cid)
                except Exception:
                    pass
            else:
                # 发不出去也记下来，别每轮都对着同一条重试
                replied.append(cid)
            # v1.9.20：不管成没成，库里那条（如果有）都标记成已回，
            # 这样回评论循环和待回复循环不会各自回一遍。
            try:
                _row = mem.find_comment_by_remote(tid, cid)
                if _row and _row.get("status") != getattr(mem, "COMMENT_REPLIED", "replied"):
                    mem.mark_comment(_row["id"], mem.COMMENT_REPLIED, _body)
            except Exception:
                pass
            if done >= 2:
                break
        if done >= 2:
            break
    st["replied"] = replied[-300:]
    st["reply_at"] = now
    save_state()
    return done, (f"回了几条评论：{done}" if done else "没有需要回的评论")




# ---------------------------------------------------------------
# 说明：这里没有 `import agent`；agent 需要的那几个名字由 agent.py 反向 import。
# ---------------------------------------------------------------
