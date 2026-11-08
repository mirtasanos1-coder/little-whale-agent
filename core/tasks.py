# -*- coding: utf-8 -*-
"""后台守护线程（从 agent.py 物理搬出来，逻辑没改）。

这里只有「驱动」：隔多久醒一次、开关关了没、到点该叫哪个 tick。
真正的动作（发消息、查动态、写日记、算配额）都还是 agent 里的函数，
本模块通过 core.globals 拿**同一份**对象和函数去调。

两条铁律（用户明确要求，别改）：
  1. 锁、会话池、状态字典一律不在这里新建 —— `_CARE_SENT` / `state` /
     `_FOLLOWUP_LOCK` / `sessions` 这些都是 agent 的那一份，靠 _sync() 拿过来。
     自己 new 一份等于两把锁，,线程之间立刻互相踩。
  2. 循环体每转一圈都 _sync() 一次 —— 配置项（MOMENT_HOURS、FEED_POLL_MIN…）
     由 agent._reload_constants() 就地刷新，只在函数入口拿一次的话，
     用户在设置里改完要重启才生效。
"""
import random
import threading
import time

import config as cfg_mod
import diary as diary_mod
import qzone as qzone_mod
from personas import active_name

from core import globals as _g
from core.utils import log  # noqa: F401

_SYNC_NAMES = ('CARE_ENABLED', 'CARE_PLAN_HOURS', 'CARE_PLAN_MAX_PER_DAY', 'CARE_PLAN_MIN_GAP_MIN', 'CARE_PLAN_RESPECT_SILENCE', 'CARE_PLAN_SOURCE', 'CHECKIN_POLL_SEC', 'COMMENT_STOP', 'DIARY_CHECK_INTERVAL_MIN', 'EVENT_GRACE_MIN', 'EVENT_SILENCE_HOURS', 'FEED_DECIDE', 'FEED_ENABLED', 'FEED_MAX_PER_ROUND', 'FEED_POLL_MIN', 'GREETING_IDLE_MIN', 'MEMORY_EXPORT_CHECK_SEC', 'MOMENT_CHECK_INTERVAL_MIN', 'MOMENT_COMMENT_DELIVER', 'MOMENT_COMMENT_POLL_MIN', 'MOMENT_DECIDE', 'MOMENT_HOURS', 'MOMENT_IMAGE_ENABLED', 'MOMENT_IMAGE_MAX', 'MOMENT_IMAGE_SOURCE', 'MOMENT_MAX_PER_DAY', 'MOMENT_MIN_PER_DAY', 'MOMENT_PROMPT', 'MOMENT_STOP', 'PRIVATE_CATCHUP_ENABLED', 'PRIVATE_CATCHUP_RECHECK_MIN', 'PROACTIVE_CHANCE', 'PROACTIVE_ENABLED', 'PROACTIVE_GROUPS', 'PROACTIVE_HOURS', 'PROACTIVE_IDLE_MIN', 'PROACTIVE_INTERVAL_MIN', 'mem', 'state', 'sessions', 'napcat', 'proactive_lock', '_FOLLOWUP', '_FOLLOWUP_LOCK', '_FOLLOWUP_INTERNAL', '_CARE_SENT', '_CARE_SENT_TTL', '_LOOP_LOG_LOCK', '_LOOP_LOG_TS', '_live_cfg', '_fnum_live', '_last_care_at', '_care_lock', '_care_allowed_now', '_care_day_allow', '_care_day_bump', '_care_gap_sec', '_checkin_should_send', '_moment_quota_today', '_care_targets', '_greeting_slot_now', '_greeting_text', '_event_checkin_text', '_affinity_decay_once', '_care_defer_clear', '_care_defer_force', '_care_defer_note', '_care_idle_try', '_care_plan_text', '_diary_snapshot_due', '_fu_ask', '_fu_load', '_fu_save', '_moment_context', '_moment_images', '_moment_recent_texts', '_moment_repeat_reason', '_moment_should_try', '_post_moment', '_proactive_extra', '_read_comments_web', '_reply_pending_comments', '_self_comment_ids', '_share_line', '_share_video_target', 'catch_up_private', 'feed_comment_round', 'feed_round', 'followup_clear', 'group_last_activity', 'last_proactive', 'load_state', 'msg_plain', 'peer_name', 'reconcile_switches', 'save_state', 'SYSTEM_PROMPT', 'ask_model', 'ask_model_raw', 'send_to_session', 'send_group_human', 'PQ_PRIO_BG')


def _sync():
    """把 agent 里那些活名字的最新值灌进本模块（每圈循环都调一次）。"""
    _g.sync(_SYNC_NAMES, globals())


# ------------------------------------------------------------------
# 搬自 agent.py L3579-3628：kg_build_loop
# ------------------------------------------------------------------
def kg_build_loop():
    """闲时把老的永久记忆一条条织进关系网。

    新记忆在写入的当下就已经带 triples进图了（diary.py里做的）；
    这里补的是升级前就存在的老记忆 ——它们只有文字，没有三元组。
    一批20~50条，一天跑一次，慢慢补齐，不抢聊天的模型额度。
    """
    _sync()
    try:
        interval = float(_live_cfg("kg_build_interval_min",1440) or 1440)
    except Exception:
        interval = 1440.0
    interval = max(30.0, interval)
    log("[图谱]建图线程启动（间隔 %.0f分钟）" % interval)
    while True:
        _sync()
        try:
            time.sleep(interval *60.0)
            #先等启动风暴过去，别一开机就打模型
            if not bool(_live_cfg("kg_build_enabled", True)):
                continue
            try:
                batch = int(_live_cfg("kg_build_batch",30) or 30)
            except Exception:
                batch = 30
            #夹在20~50：太少会永远补不完，太多会一次吃光额度
            batch = max(20, min(batch,50))
            import kg as _kg
            if not _kg.available():
                log("[图谱]没装 networkx，建图线程跳过（pip install networkx开启）", "DEBUG")
                continue
            import diary as _diary
            accounts = []
            try:
                accounts = _diary.bound_accounts()
            except Exception:
                accounts = []
            if not accounts:
                continue
            for uid in accounts:
                try:
                    res = _kg.build_from_perm(mem, uid, limit=batch,
                        model_ask=_diary._call_model)
                    if res.get("added"):
                        log("[图谱] %s补建 %s条关系（%s）"
                            % (uid, res.get("added"), res.get("msg")), "INFO")
                    else:
                        log("[图谱] %s: %s" % (uid, res.get("msg")), "DEBUG")
                except Exception as e:
                    log("[图谱] %s建图失败: %s" % (uid, e), "WARN")
        except Exception as e:
            log("[图谱]建图线程异常: %s" % e, "WARN")


# ------------------------------------------------------------------
# 搬自 agent.py L3646-3707：diary_loop
# ------------------------------------------------------------------
def diary_loop():
    """v1.9.10：按 diary_interval_value/unit 的间隔写「记忆快照」（原日记）。

    为什么要改：一天一篇的日记，一旦上下文被截断/清空，她就有大半天「失忆」。
    改成 4 小时一篇的短快照后，任何时候都能从最近几篇里接上话。
    """
    _sync()
    log(f"[日记] 线程启动（间隔 {int(diary_mod.diary_interval_sec() / 60)} 分钟，"
        f"最少 {_live_cfg('diary_trigger_min_msgs', 10)} 条消息才写）")
    while True:
        _sync()
        try:
            time.sleep(DIARY_CHECK_INTERVAL_MIN * 60)
            if not _diary_snapshot_due():
                continue
            today = time.strftime("%Y-%m-%d")
            slot = time.strftime("%H:%M")
            done = []
            for uid in diary_mod.bound_accounts():
                name = peer_name(uid)
                res = diary_mod.write_diary_for(mem, uid, name, slot=slot)
                if res.get("ok") and res.get("id"):
                    done.append(uid)
                    log(f"[日记] {uid}({name}) → 《{res.get('title')}》{res.get('mood')}", "INFO")
                else:
                    log(f"[日记] {uid} 跳过：{res.get('msg')}", "DEBUG")
            if done:
                # 注意：save_state() 不吃参数，它只把模块级 state 的白名单字段写盘
                state["diary_wrote"] = {"day": today, "count": len(done), "slot": slot}
                state["diary_last_ts"] = time.time()
                save_state()
            # v1.1.1：日记写完后顺手「学一次」——从今天的聊天里提炼经验。
            # v1.9.23：以前这整段缩在 `if done:` 里，日记因为「今天消息少」而跳过时，
            # 学习就跟着一次都不跑（用户看到的就是「学习从来没触发过」）。
            # 现在日记写没写成都会试一次，聊天够不够由 learn_from_dialog 自己判断。
            if bool(cfg_mod.get("learn_auto", True)):
                try:
                    import learn as learn_mod
                    for uid in (done or diary_mod.bound_accounts()):
                        r = learn_mod.learn_from_dialog(
                            mem, uid, name=peer_name(uid),
                            model_caller=lambda p: ask_model_raw(
                                p, system=SYSTEM_PROMPT,
                                max_tokens=400, temperature=0.4,
                                prio=PQ_PRIO_BG))
                        log(f"[学习] {uid} → {r.get('msg')}", "INFO")
                except Exception as ex:
                    log(f"[学习] 自动学习失败：{ex}", "WARN")
            if done:
                # v1.1.4 3.4：每日全量总结（把所有绑定账号今天的聊天一起看一遍）
                # 仍然只在日记写成时做一次，避免每 4 小时重复总结同一批聊天。
                if bool(cfg_mod.get("learn_auto", True)):
                    try:
                        import learn as learn_mod2
                        _sum = learn_mod2.summarize_day(
                            mem, model_caller=lambda p: ask_model_raw(
                                p, max_tokens=700, temperature=0.4, tag="/今日总结",
                                prio=PQ_PRIO_BG))
                        log(f"[学习·每日总结] {_sum.get('msg')}", "INFO")
                    except Exception as ex:
                        log(f"[学习·每日总结] 失败：{ex}", "WARN")
        except Exception as e:
            log(f"[日记] 循环异常: {e}", "ERROR")
            time.sleep(60)


# ------------------------------------------------------------------
# 搬自 agent.py L3730-3769：share_video_loop
# ------------------------------------------------------------------
def share_video_loop():
    """她「刷到」一个有意思的视频，主动发给最近聊过的人。

    三道闸：①开关 ②最小间隔（share_video_gap_min，默认 90 分钟）
    ③不在深夜（23 点~8 点不发）。宁可少发，也不打扰。
    """
    _sync()
    log("[视频分享] 线程启动")
    while True:
        _sync()
        try:
            time.sleep(600)
            if not bool(cfg_mod.get("agent_share_video_enabled", True)):
                continue
            hour = time.localtime().tm_hour
            if hour >= 23 or hour < 8:
                continue
            st = load_state()
            gap = float(_live_cfg("share_video_gap_min", 90) or 90) * 60.0
            if (time.time() - float(st.get("share_video_ts") or 0)) < max(600.0, gap):
                continue
            sk = _share_video_target()
            if not sk:
                continue
            import videocatch as _vc
            items = _vc.hot_bilibili(12)
            if not items:
                continue
            import random as _rnd
            it = _rnd.choice(items[:12])
            line = _share_line(it)
            if not line:
                continue
            # QA P2-1：分享只有一句，拆条没有拟人收益，还容易被切在《》里
            if not send_to_session(sk, line, no_split=True, remember=True):
                continue
            state["share_video_ts"] = time.time()
            save_state()
            log("[视频分享] 发给 %s：%s" % (sk, str(it.get("title"))[:60]), "INFO")
        except Exception as e:
            log("[视频分享] 循环异常（忽略）：%s" % e, "DEBUG")
            time.sleep(120)


# ------------------------------------------------------------------
# 搬自 agent.py L3803-3847：memory_export_loop
# ------------------------------------------------------------------
def memory_export_loop():
    """按设置里的周期，把记忆整目录导出到 memory_backup/（只留最近 30 份）。

    为什么单独开线程而不是塞进 diary_loop：
      日记是「每天写一次」，导出周期可能是秒/分钟级（用户想把周期调很短来测试），
      两者时间尺度差太多，混在一起会出现「导出被日记的日检查卡住」。
    """
    _sync()
    try:
        import mem_transfer
    except Exception as e:
        log(f"[记忆备份] 模块不可用，定时导出关闭：{e}", "WARN")
        return
    log(f"[记忆备份] 线程启动（每分钟检查一次，当前设置："
        f"{'开' if bool(cfg_mod.get('memory_export_enabled', False)) else '关'}，"
        f"每 {cfg_mod.get('memory_export_every')} {cfg_mod.get('memory_export_unit')}）")
    while True:
        _sync()
        try:
            time.sleep(MEMORY_EXPORT_CHECK_SEC)
            # ① 「丢进 memory_import/ 就自动导入」：合并模式，导完挪进 done/
            try:
                _imp = mem_transfer.scan_import_dir(mem=mem, log=log)
                if _imp.get("ok_count"):
                    log(f"[记忆导入] 自动导入了 {_imp.get('ok_count')} 个文件")
            except Exception as ex:
                log(f"[记忆导入] 扫描目录失败: {ex}", "WARN")
            # ② 到点了才做定时导出
            # ② 到点了才做定时导出（记忆，默认关）
            if bool(cfg_mod.get("memory_export_enabled", False)):
                e = mem_transfer.auto_export_tick(mem=mem, log=log)
                if e.get("did") and not e.get("ok"):
                    log(f"[记忆备份] 自动导出失败：{e.get('msg')}", "WARN")
            # ③ 日记备份：独立开关、独立周期、独立目录（和记忆分开存）
            if bool(cfg_mod.get("diary_export_enabled", False)):
                try:
                    d = mem_transfer.diary_export_tick(mem=mem, log=log)
                    if d.get("did"):
                        if d.get("ok"):
                            log(f"[日记备份] {d.get('msg')}")
                        else:
                            log(f"[日记备份] 失败：{d.get('msg')}", "WARN")
                except Exception as ex:
                    log(f"[日记备份] 异常：{ex}", "WARN")
        except Exception as e:
            log(f"[记忆备份] 循环异常: {e}", "ERROR")
            time.sleep(60)


# ------------------------------------------------------------------
# 搬自 agent.py L5812-5829：_loop_log
# ------------------------------------------------------------------
def _loop_log(tag, msg, key="", gap=300):
    """打一条节流 INFO。tag 是日志前缀，key 用来去重（默认用 msg 本身）。"""
    _sync()
    try:
        _now = time.time()
        _k = str(tag or "") + "|" + str(key or msg or "")
        with _LOOP_LOG_LOCK:
            _last = _LOOP_LOG_TS.get(_k, 0)
            if _last and (_now - _last) < max(0.0, float(gap or 0)):
                return
            _LOOP_LOG_TS[_k] = _now
            if len(_LOOP_LOG_TS) > 400:
                _cut = _now - 6 * 3600
                _dead = [y for y, t in _LOOP_LOG_TS.items() if float(t or 0) < _cut]
                for _x in _dead:
                    _LOOP_LOG_TS.pop(_x, None)
        log("[" + str(tag or "循环") + "] " + str(msg), "INFO")
    except Exception:
        pass


# ------------------------------------------------------------------
# 搬自 agent.py L6029-6119：followup_loop
# ------------------------------------------------------------------
def followup_loop():
    """后台守护线程：对方没回话，就隔几分钟追问一句，直到话题结束。"""
    _sync()
    log("[追问] 线程已启动（对方没回就隔几分钟追问一句）", "INFO")
    try:
        _fu_load()
    except Exception:
        pass
    time.sleep(20)
    while True:
        _sync()
        try:
            if not bool(_live_cfg("followup_enabled", True)):
                time.sleep(60)
                _loop_log("追问", "「没回就追问」开关是关的，先歇一会儿", key="fu_off", gap=1800)
                continue
            gap_min = float(_fnum_live("followup_gap_min", 8) or 8)
            gap_min = max(1.0, min(gap_min, 240.0))
            max_n = int(max(1, min(6, int(_fnum_live("followup_max", 3) or 3))))
            win_min = float(_fnum_live("followup_window_min", 180) or 180)
            win_min = max(10.0, min(win_min, 1440.0))
            time.sleep(40)
            now = time.time()
            with _FOLLOWUP_LOCK:
                items = [(k, dict(v)) for k, v in _FOLLOWUP.items()]
            if not items:
                continue
            for sk, rec in items:
                try:
                    t = float(rec.get("t") or 0)
                    n = int(rec.get("n") or 0)
                    if not t:
                        followup_clear(sk, "时间戳缺失")
                        continue
                    waited = now - t
                    if n >= max_n:
                        _loop_log("追问", "已经追问 %d 次，不再追了（%s）" % (n, sk), key="fu_max|" + sk, gap=3600)
                        continue
                    if waited < gap_min * 60:
                        continue
                    if waited > win_min * 60:
                        followup_clear(sk, "超过 %d 分钟没回，算话题结束" % int(win_min))
                        log(f"[追问] {sk} 超过 {win_min:.0f} 分钟没回，算话题结束，不再追问", "INFO")
                        continue
                    try:
                        last_u = mem.last_message_ts(sk, role="user") or 0
                    except Exception:
                        last_u = 0
                    if last_u and last_u >= t:
                        followup_clear(sk, "对方其实回过")
                        continue
                    if not _care_allowed_now():
                        _loop_log("追问", "现在不在「主动回复时段」里，先不追问", key="fu_hours", gap=1800)
                        continue
                    with _care_lock:
                        if now - _last_care_at.get(sk, 0) < _care_gap_sec():
                            _loop_log("追问", "刚发过主动消息，等隔够了再追问（" + sk + "）", key="fu_gap|" + sk, gap=900)
                            continue
                    text = _fu_ask(rec, gap_min, n, sk)
                    if text == "END":
                        followup_clear(sk, "模型判断话题结束了")
                        log(f"[追问] {sk} 模型判断话题已结束，不再追问", "INFO")
                        continue
                    if not text:
                        continue
                    if not _care_day_allow():
                        log("[追问] 今天的主动消息次数已到上限，这次不追问", "INFO")
                        continue
                    global _FOLLOWUP_INTERNAL
                    # v1.9.26 拆分：这个标志也在写回 agent ——
                    # 读它的 _fu_ask 还留在 agent 里，两边必须是同一个值。
                    _FOLLOWUP_INTERNAL = True
                    _g.set_live("_FOLLOWUP_INTERNAL", True)
                    try:
                        ok = send_to_session(sk, text, remember=True)
                    finally:
                        _FOLLOWUP_INTERNAL = False
                        _g.set_live("_FOLLOWUP_INTERNAL", False)
                    if ok:
                        with _care_lock:
                            _last_care_at[sk] = now
                        with _FOLLOWUP_LOCK:
                            cur = dict(_FOLLOWUP.get(sk) or {})
                            cur["t"] = int(now)
                            cur["n"] = n + 1
                            cur["q"] = text[:200]
                            _FOLLOWUP[sk] = cur
                        _fu_save()
                        _care_day_bump()
                        log(f"[追问] 第 {n + 1} 次追问 -> {sk}（等了 {waited / 60:.0f} 分钟）: {text[:60]}", "INFO")
                    else:
                        log(f"[追问] {sk} 发送失败，下轮再试", "WARN")
                except Exception as e:
                    log(f"[追问] 处理 {sk} 出错（跳过）: {e}", "WARN")
        except Exception as e:
            log(f"[追问] 循环异常: {e}", "ERROR")
            time.sleep(30)


# ------------------------------------------------------------------
# 搬自 agent.py L6122-6228：care_loop
# ------------------------------------------------------------------
def care_loop():
    """后台守护线程：定时问候 + 事件查岗"""
    # v1.1.4：开关与扫描间隔都改成实时读配置。
    # 以前这里 `if not CARE_ENABLED: return` 会让线程直接退出——
    # 用户在设置里刚打开「主动回复」，必须重启才生效，看着就像「从没触发」。
    _sync()
    log("[关怀] 线程已启动（开关/间隔实时读配置）", "INFO")
    time.sleep(10)

    while True:
        _sync()
        try:
            if not bool(_live_cfg("care_plan_enabled", True)):
                time.sleep(30)
                _loop_log("关怀", "「主动回复」总开关是关的，先歇 30 秒再看", key="cp_off", gap=900)
                continue
            if not bool(_live_cfg("care_enabled", CARE_ENABLED)):
                time.sleep(30)
                _loop_log("关怀", "「主动关怀」开关是关的，先歇 30 秒再看", key="care_off", gap=900)
                continue
            try:
                _poll = float(_live_cfg("checkin_poll_sec", CHECKIN_POLL_SEC) or CHECKIN_POLL_SEC)
            except (TypeError, ValueError):
                _poll = float(CHECKIN_POLL_SEC or 30)
            time.sleep(max(5.0, _poll))
            if not _care_allowed_now():
                _loop_log("关怀", "现在不在「主动回复时段」里，等下一轮", key="care_hours", gap=1800)
                continue
            now = time.time()

            # ---------- 1) 定时问候 ----------
            slot = _greeting_slot_now()
            if slot:
                slot_key, slot_label = slot
                for sk in _care_targets():
                    gkey = f"{sk}|{slot_key}"
                    if state["greeted"].get(gkey):
                        _loop_log("关怀", "这个时段的问候今天已经发过了（" + str(slot_label) + "）", key="greet_done|" + gkey, gap=3600)
                        continue
                    # 会话要安静够久才问候（"如果群/私聊没有动静"）
                    last = mem.last_message_ts(sk)
                    if last and (now - last) < GREETING_IDLE_MIN * 60:
                        _loop_log("关怀", "会话还有动静（" + sk + "），没安静够 " + str(GREETING_IDLE_MIN) + " 分钟，先不问候", key="greet_busy|" + sk, gap=900)
                        continue
                    with _care_lock:
                        if now - _last_care_at.get(sk, 0) < _care_gap_sec():
                            _loop_log("关怀", "距上次主动发言不到 " + str(int(_care_gap_sec() / 60)) + " 分钟（" + sk + "），跳过", key="gap_greet|" + sk, gap=900)
                            continue
                        _last_care_at[sk] = now
                    text = _greeting_text(slot_label, sk)
                    if not _care_day_allow():
                        log("[关怀] 今天的主动消息次数已到上限，跳过问候", "INFO")
                        continue
                    if send_to_session(sk, text, remember=True):
                        _care_day_bump()
                        state["greeted"][gkey] = int(now)
                        save_state()
                        log(f"[关怀] 定时问候 {slot_label} -> {sk}: {text[:60]}", "INFO")

            # ---------- 2) 事件查岗 ----------
            try:
                due = mem.get_due_events(now=now, grace_sec=EVENT_GRACE_MIN * 60)
            except Exception as e:
                log(f"[查岗] 读取事件失败: {e}", "ERROR")
                due = []

            for ev in due:
                sk = ev.get("session_key") or f"private_{ev['user_id']}"
                ok_send, why = _checkin_should_send(sk, ev)
                if not ok_send:
                    log(f"  [查岗] 事件 #{ev['id']} 到期但先不发：{why}", "INFO")
                    continue
                with _care_lock:
                    if now - _last_care_at.get(sk, 0) < _care_gap_sec():
                        _loop_log("关怀", "距上次主动发言不到 " + str(int(_care_gap_sec() / 60)) + " 分钟（" + sk + "），这次查岗先不发", key="gap_ev|" + sk, gap=900)
                        continue
                    _last_care_at[sk] = now
                try:
                    last = mem.last_message_ts(sk, role="user") or mem.last_message_ts(sk) or ev["target_time"]
                    hours_silent = max(0.0, (now - last) / 3600.0)
                except Exception:
                    hours_silent = EVENT_SILENCE_HOURS
                text = _event_checkin_text(ev["event_desc"], hours_silent, sk)
                if not _care_day_allow():
                    log("[查岗] 今天的主动消息次数已到上限，事件留到明天", "INFO")
                    continue
                if send_to_session(sk, text, remember=True):
                    _care_day_bump()
                    mem.mark_event_done(ev["id"])
                    log(f"[查岗] 事件 #{ev['id']} 到期 -> {sk}: {text[:60]} "
                        f"（静默 {hours_silent:.1f}h，{why}，已标记 done）", "INFO")
                else:
                    log(f"  [查岗] 事件 #{ev['id']} 发送失败，保持 pending 下轮再试", "WARN")

            # ---------- 3) 说说配额跨天重置 ----------
            # 每轮都调一次，保证 0 点后立刻换新的随机配额
            try:
                _moment_quota_today()
            except Exception:
                pass

            # ---------- 4) 好感度每日衰减 ----------
            try:
                _affinity_decay_once()
            except Exception as e:
                log(f"[好感度] 衰减检查失败: {e}", "DEBUG")

        except Exception as e:
            log(f"[关怀] 循环异常: {e}", "ERROR")


# ------------------------------------------------------------------
# 搬自 agent.py L6232-6296：proactive_loop
# ------------------------------------------------------------------
def proactive_loop():
    _sync()
    if not PROACTIVE_ENABLED:
        _loop_log("自主发言", "PROACTIVE_ENABLED 是关的，这个线程不启动", key="pa_off", gap=0)
        return

    log(f"自主发言：已启用，间隔 >= {PROACTIVE_INTERVAL_MIN} 分钟，"
        f"时段 {PROACTIVE_HOURS[0]}:00-{PROACTIVE_HOURS[1]}:00")
    time.sleep(60)

    while True:
        _sync()
        try:
            time.sleep(120)
            now = time.time()
            hour = time.localtime().tm_hour
            if not (PROACTIVE_HOURS[0] <= hour < PROACTIVE_HOURS[1]):
                _loop_log("自主发言", "现在不在 " + str(PROACTIVE_HOURS[0]) + ":00-" + str(PROACTIVE_HOURS[1]) + ":00 时段里", key="pa_hours", gap=3600)
                continue

            for gid in PROACTIVE_GROUPS:
                with proactive_lock:
                    last = last_proactive.get(gid, 0)
                    idle = now - group_last_activity.get(gid, now)

                if now - last < PROACTIVE_INTERVAL_MIN * 60:
                    _loop_log("自主发言", "群 " + str(gid) + " 距上次冒泡还不到 " + str(PROACTIVE_INTERVAL_MIN) + " 分钟", key="pa_gap|" + str(gid), gap=1800)
                    continue
                if idle < PROACTIVE_IDLE_MIN * 60:
                    _loop_log("自主发言", "群 " + str(gid) + " 刚有人说话，等它安静 " + str(PROACTIVE_IDLE_MIN) + " 分钟", key="pa_idle|" + str(gid), gap=1800)
                    continue
                if random.random() > PROACTIVE_CHANCE:
                    _loop_log("自主发言", "群 " + str(gid) + " 这次没抽中（概率 " + str(int(PROACTIVE_CHANCE * 100)) + "%）", key="pa_chance|" + str(gid), gap=1800)
                    continue

                topic = ""
                try:
                    r = napcat("get_group_msg_history", group_id=gid, count=8)
                    if r and r.get("status") == "ok":
                        recent = (r.get("data") or {}).get("messages") or []
                        lines = []
                        for m in recent[-6:]:
                            nm = (m.get("sender", {}).get("card")
                                  or m.get("sender", {}).get("nickname") or "")
                            pl = msg_plain(m.get("message"))
                            if pl:
                                lines.append(f"{nm}: {pl[:60]}")
                        topic = "\n".join(lines)
                except Exception:
                    pass

                prompt = (
                    "群里已经安静好一会儿了。\n"
                    + (f"最近的聊天内容：\n{topic}\n\n" if topic else "")
                    + "请你主动在群里说一句话，像群里的一员那样自然冒泡，"
                      "可以接上面的话题，也可以只是随口一句。"
                      "一句话，不超过 40 字，不要每次都提鲸鱼。"
                    + _proactive_extra()
                )
                reply = ask_model([], prompt, scope=f"group_{gid}", allow_fallback=False, detail=f"群聊冒泡 · 群 {gid}")
                if reply:
                    send_group_human(gid, reply)
                    with proactive_lock:
                        last_proactive[gid] = now
                    log(f"自主发言 -> 群 {gid}: {reply[:60]}")
        except Exception as e:
            log(f"自主发言异常: {e}", "ERROR")


# ------------------------------------------------------------------
# 搬自 agent.py L6779-6793：private_catchup_loop
# ------------------------------------------------------------------
def private_catchup_loop():
    """后台线程：每隔一段时间再扫一次私聊，兜住"WS 漏推"的情况。

    正常消息走 WS 实时处理，这里只是保险丝：
    NapCat 重启、网络抖动时 WS 可能漏事件，靠这个把漏掉的私聊补回来。
    """
    _sync()
    if not PRIVATE_CATCHUP_ENABLED:
        return
    time.sleep(120)   # 启动时已经跑过一次，这里先等两分钟
    while True:
        _sync()
        try:
            time.sleep(PRIVATE_CATCHUP_RECHECK_MIN * 60)
            catch_up_private()
        except Exception as e:
            log(f"[私聊补回] 循环异常: {type(e).__name__}: {e}", "ERROR")


# ------------------------------------------------------------------
# 搬自 agent.py L7133-7145：_switch_supervisor_loop
# ------------------------------------------------------------------
def _switch_supervisor_loop(interval=5.0):
    """每 5 秒对一次账（只读配置 + 看线程活没活，几乎不耗性能）。"""
    _sync()
    while True:
        _sync()
        try:
            acts = reconcile_switches()
            if acts:
                log(f"[开关] 已按最新设置调整：{acts}")
        except Exception as e:                 # noqa: BLE001
            try:
                log(f"[开关] 对账出错（{type(e).__name__}: {e}）", "WARN")
            except Exception:
                pass
        time.sleep(max(2.0, float(interval or 5.0)))


# ------------------------------------------------------------------
# 搬自 agent.py L7148-7259：moment_loop
# ------------------------------------------------------------------
def moment_loop():
    """后台线程：定时评估要不要发说说（v1.6.1：开关实时生效，关掉就退出）"""
    _sync()
    if not _live_cfg("moment_enabled", True):
        log("[说说] 已禁用")
        return

    # v1.6.1（QA P0-2）：这里原本会先 `qzone_mod.caps()` 探一次 NapCat 能力，
    # 那是个网络调用 —— 没开 NapCat 时要卡几十秒，这期间用户关掉开关，
    # 线程还在 caps() 里出不来，看起来就「关了还在跑」。能力探测挪到真要发的时候。
    log(f"[说说] 已启用：每天 "
        + (f"{MOMENT_MIN_PER_DAY} 条（固定）" if MOMENT_MIN_PER_DAY == MOMENT_MAX_PER_DAY
           else f"{MOMENT_MIN_PER_DAY}~{MOMENT_MAX_PER_DAY} 条随机")
        + f"，时段 {MOMENT_HOURS[0]}:00-{MOMENT_HOURS[1]}:00，"
        f"每 {MOMENT_CHECK_INTERVAL_MIN} 分钟评估一次，"
        f"决策方式={'模型自己决定' if MOMENT_DECIDE == 'model' else '到点就发'}")
    log(f"[说说] 配图：{'开（' + MOMENT_IMAGE_SOURCE + '，最多 ' + str(MOMENT_IMAGE_MAX) + ' 张）' if MOMENT_IMAGE_ENABLED else '关'}")
    if MOMENT_STOP.wait(90):        # 启动后先别急着发；被叫停就不用等了
        log("[说说] 已禁用，线程休眠")
        return

    while True:
        _sync()
        try:
            # v1.6.3（QA P2-4）：间隔读实时配置，改设置立刻生效（原来写死60秒）
            _gap_s = _fnum_live("moment_check_interval_min", 17)
            _gap_s = max(1.0, float(_gap_s)) * 60.0
            #最低60秒：别让配置写成0把循环变成满 CPU空转
            _gap_s = max(60.0, _gap_s)
            if MOMENT_STOP.wait(_gap_s):
                log("[说说] 已禁用，线程休眠")
                return
            if not _live_cfg("moment_enabled", True):
                log("[说说] 已禁用，线程休眠")
                return
            if not _moment_should_try():
                continue
            m = state.setdefault("moment", {})
            quota = _moment_quota_today()
            used = int(m.get("count") or 0)

            prompt = MOMENT_PROMPT.format(
                name=active_name(),
                now=time.strftime("%Y-%m-%d %H:%M %A"),
                context=_moment_context(),
            )
            if MOMENT_DECIDE != "model":
                # timer 模式：到点就必须发一条，不允许模型跳过（v3 老行为）
                prompt += "\n（到点了，这次不用判断要不要发，直接写一条。）"

            recent = _moment_recent_texts(state, 3)
            text, dup_reason = "", ""
            for attempt in range(3):
                ask = prompt
                if attempt:
                    ask += ("\n（提醒：刚才那版跟最近发过的太像 —— " + dup_reason +
                            "。换一个完全不同的话题和句式重写，结尾也别再用同一个表情。）")
                try:
                    text = ask_model_raw(ask, max_tokens=300, temperature=0.95,
                                         effort="none", tag="/说说", prio=PQ_PRIO_BG)
                except Exception as e:
                    log(f"[说说] 生成失败: {e}", "ERROR")
                    text = ""
                    break

                text = (text or "").strip().strip('"').strip("'").strip()
                # 模型判断"今天没什么可发的"
                if MOMENT_DECIDE == "model" and (
                        not text or text.upper().startswith("SKIP")
                        or text in ("跳过", "不发")):
                    log(f"[说说] 模型判断本次不发（今日 {used}/{quota}）", "DEBUG")
                    text = ""
                    break
                if not text:
                    break
                text = text[:300]
                dup_reason = _moment_repeat_reason(text, recent)
                if not dup_reason:
                    break
                log(f"[说说] 这版重复了（{dup_reason}），让它换个说法重写", "INFO")
            if text and dup_reason:
                # 连写几版都撞车：这一轮干脆不发，别往空间里塞雷同内容
                log(f"[说说] 连写 3 版都重复（{dup_reason}），本轮不发", "INFO")
                text = ""
            if not text:
                m["last_at"] = int(time.time())
                save_state()
                continue

            # v6：配图
            imgs = []
            try:
                imgs = _moment_images(text)
            except Exception as e:
                log(f"[说说] 挑配图失败（不带图发）: {e}", "DEBUG")
            ok, tid, msg = _post_moment(text, imgs)
            m["last_at"] = int(time.time())
            if ok:
                m["count"] = used + 1
                m.setdefault("log", []).append(
                    {"ts": int(time.time()), "text": text[:80],
                     "tid": tid, "images": len(imgs)})
                m["log"] = m["log"][-20:]
                m["last_tid"] = tid
                m["last_text"] = text[:300]
                extra = f"（配图 {len(imgs)} 张）" if imgs else ""
                log(f"[说说] 已发布{extra}（今日 {m['count']}/{quota}）："
                    f"{text[:80]}", "INFO")
            else:
                log(f"[说说] 发布失败: {msg}", "ERROR")
            save_state()

        except Exception as e:
            log(f"[说说] 循环异常: {type(e).__name__}: {e}", "ERROR")


# ------------------------------------------------------------------
# 搬自 agent.py L7633-7709：moment_comment_loop
# ------------------------------------------------------------------
def moment_comment_loop():
    """后台线程：每 N 分钟看一眼说说评论，有就回（v1.6.1：开关实时生效）。"""
    _sync()
    if not _live_cfg("moment_comment_enabled", False):
        log("[评论] 已禁用")
        return
    if not mem:
        return
    # v1.6.1（QA P0-2）：别在这里探 caps —— 那是网络调用，没开 NapCat 时会卡
    # 几十秒，期间用户关掉开关，线程还在里面出不来（表现就是「关了还在跑」）。
    # 能力探测放到真要读评论/回评论的时候（那里本来就有 try/except）。
    log(f"[评论] 已启用：每 {MOMENT_COMMENT_POLL_MIN} 分钟检查一次，"
        f"回复方式={MOMENT_COMMENT_DELIVER}")
    try:
        qzone_mod.ensure_inbox()
    except Exception:
        pass

    while True:
        _sync()
        try:
            if COMMENT_STOP.wait(max(60, int(_live_cfg("moment_comment_poll_min", 10)) * 60)):
                log("[评论] 已禁用，线程休眠")
                return
            if not _live_cfg("moment_comment_enabled", False):
                log("[评论] 已禁用，线程休眠")
                return
            # v1.9.20：网页协议优先 —— NapCat 那边读评论/回评论接口都不存在，
            # 每次先探它只会白刷一行「不支持的Api」，还慢。
            items, msg = [], ""
            try:
                items = _read_comments_web()
                if items:
                    msg = "网页协议读到 %d 条" % len(items)
            except Exception as e:
                log(f"[评论] 网页协议读取异常：{e}", "DEBUG")
            if not items:
                ok, items, msg = qzone_mod.read_comments()
                if not ok:
                    log(f"[评论] 读取失败: {msg}", "DEBUG")
                    continue
            _suins, _snames = _self_comment_ids()
            if _suins or _snames:
                _loop_log("评论", "自己的号：" + ("、".join(sorted(_suins)) or "（没拿到，靠回声判定兜底）"),
                        key="cc_self", gap=3600)
            new_n = 0
            for it in items:
                _cuid = str(it.get("commenter_id") or "").strip()
                _cnm = str(it.get("commenter_name") or "").strip()
                # v1.9.9：她自己发的评论会被接口一起读回来，先摘出去，否则自己回自己
                if _cuid and _cuid in _suins:
                    log("[评论] 跳过自己发的评论（uin " + _cuid + "）：" + str(it.get("content", ""))[:40], "DEBUG")
                    continue
                if _snames and not _cuid and _cnm in _snames:
                    log("[评论] 跳过自己发的评论（昵称 " + _cnm + "）：" + str(it.get("content", ""))[:40], "DEBUG")
                    continue
                try:
                    cid, is_new = mem.add_comment(
                        content=it.get("content", ""),
                        commenter_id=it.get("commenter_id", ""),
                        commenter_name=it.get("commenter_name", ""),
                        moment_tid=it.get("moment_tid", ""),
                        moment_text=it.get("moment_text", ""),
                        source=it.get("source", "napcat"),
                        remote_id=it.get("remote_id", ""),
                    )
                except Exception as e:
                    log(f"[评论] 落库失败: {e}", "WARN")
                    continue
                if is_new:
                    new_n += 1
                    log(f"[评论] 新评论 #{cid} "
                        f"{it.get('commenter_name') or it.get('commenter_id')}："
                        f"{it.get('content', '')[:60]}", "INFO")
            if new_n:
                log(f"[评论] 本轮收到 {new_n} 条新评论")
            _reply_pending_comments(limit=5)
        except Exception as e:
            log(f"[评论] 循环异常: {type(e).__name__}: {e}", "ERROR")


# ------------------------------------------------------------------
# 搬自 agent.py L8141-8184：feed_loop
# ------------------------------------------------------------------
def feed_loop():
    """后台线程：定时刷动态 + 追评论。"""
    _sync()
    _live_feed = _live_cfg("feed_enabled", FEED_ENABLED)
    _live_reply = _live_cfg("feed_reply_enabled", True)
    if not _live_feed and not _live_reply:
        log("[动态] 已禁用（刷动态和回评论都关着）")
        return
    try:
        cap = qzone_mod.caps()
    except Exception:
        cap = {}
    _live_poll = max(1, int(_live_cfg("feed_poll_min", FEED_POLL_MIN) or FEED_POLL_MIN))
    log(f"[动态] 已启用：启动后先刷一轮，之后每 {_live_poll} 分钟刷一次，"
        f"互动方式={FEED_DECIDE}，每轮最多 {FEED_MAX_PER_ROUND} 条；"
        f"NapCat 能力 点赞={cap.get('like')} 读动态={cap.get('read')} "
        f"发评论={cap.get('reply')}")

    if not cap.get("read"):
        log(f"[动态] NapCat 读不到好友动态，改走空间网页协议；"
            f"也可以把内容放进 feed_inbox/（.json）", "WARN")

    while True:
        _sync()
        try:
            _poll = max(1, int(_live_cfg("feed_poll_min", FEED_POLL_MIN) or FEED_POLL_MIN))
            # v1.1.7 修（P0-2：启动后要干等一整个轮询周期才会刷第一条）：
            # 老顺序是 while True 里先 time.sleep(_poll*60) 再干活，
            # 而 _poll 又被 max(60, …) 夹到至少 60 分钟 —— 于是「刚打开半小时
            # 一条动态都没读」完全正常。改成先刷一轮，再按周期睡。
            if _live_cfg("feed_enabled", FEED_ENABLED):
                n, msg = feed_round()
                if n:
                    log(f"[动态] {msg}")
            # 回评论独立于「刷别人动态」：只手动开了回评论也能跑
            try:
                rn, rmsg = feed_comment_round()
                if rn:
                    log(f"[动态] {rmsg}")
            except Exception as e:
                log(f"[动态] 回评论异常: {e}", "WARN")
            time.sleep(_poll * 60)
        except Exception as e:
            log(f"[动态] 循环异常: {type(e).__name__}: {e}", "ERROR")
            # 出错也要歇一下，否则异常连发时会变成满 CPU 的死循环
            time.sleep(30)


# ------------------------------------------------------------------
# 搬自 agent.py L8478-8598：care_plan_loop
# ------------------------------------------------------------------
def care_plan_loop():
    """后台线程：到点执行模型排好的"主动关心"计划。"""
    _sync()
    if not bool(_live_cfg("care_plan_enabled", True)):
        log("[关心计划] 已禁用")
        return
    if not mem:
        return
    log(f"[关心计划] 已启用：来源={CARE_PLAN_SOURCE}，"
        f"时段 {CARE_PLAN_HOURS[0]}:00-{CARE_PLAN_HOURS[1]}:00，"
        f"每天最多 {CARE_PLAN_MAX_PER_DAY} 条，"
        f"最小间隔 {CARE_PLAN_MIN_GAP_MIN} 分钟")

    while True:
        _sync()
        try:
            # 每轮现算等待时间：改了「检查间隔」不用重启（原来写死的是 import 期的值）
            _poll = max(10, min(3600, int(_fnum_live("checkin_poll_sec", 60))))
            # 「静默 N 分钟就开口」得比 N 更勤地看，否则永远慢半拍
            _sil = float(_fnum_live("checkin_silence_hours", 2.0))
            if _sil and _sil * 3600 < _poll * 6:
                _poll = max(10, int(_sil * 3600 / 2))
            time.sleep(_poll)
            if len(_CARE_SENT) >200:
                _cut = time.time() - _CARE_SENT_TTL
                for _k in [x for x, v in _CARE_SENT.items() if float(v[1] or 0) < _cut]:
                    _CARE_SENT.pop(_k, None)
            now = time.localtime()
            _hrs = _live_cfg("care_plan_hours", [8, 23]) or [8, 23]
            try:
                _h0, _h1 = int(_hrs[0]), int(_hrs[1])
            except Exception:
                _h0, _h1 = 8, 23
            if not (_h0 <= now.tm_hour < _h1):
                _loop_log("关心计划", "现在不在 " + str(_h0) + ":00-" + str(_h1) + ":00 时段里", key="cp_hours", gap=3600)
                continue
            due = mem.get_due_plans()
            if not due:
                # v1.8.11：没有到点的计划，就看看是不是该静默主动开口
                _care_idle_try()
                _loop_log("关心计划", "暂时没有到点的关心计划", key="cp_none", gap=3600)
                continue
            used = mem.count_plans_today()
            _max_day = int(_fnum_live("care_plan_max_per_day", 6))
            if used >= _max_day:
                log(f"[关心计划] 今日已发 {used} 条，达到上限，暂停", "INFO")
                # 到点的先标成 done，避免一直堆积
                for p in due:
                    mem.mark_plan_done(p["id"])
                    _care_defer_clear(p["id"])
                continue
            last = mem.last_plan_ts()
            _gap_min = float(_fnum_live("care_plan_min_gap_min", 25))
            gap_block = bool(last and (time.time() - last) < _gap_min * 60)
            for p in due[:3]:
                if used >= _max_day:
                    break
                sk = str(p.get("session_key") or "")
                # v1.9.9：这条是用户指定的时间点（「下午四点叫我」），不看静默
                _ign = int(p.get("ignore_silence") or 0)
                if not sk:
                    mem.mark_plan_done(p["id"])
                    _care_defer_clear(p["id"])
                    _loop_log("关心计划", "计划 #" + str(p.get("id")) + " 没有会话，标记完成跳过", key="cp_nosk|" + str(p.get("id")), gap=0)
                    continue
                # 全局最小间隔只管"模型自己排的"；界面上手动加的按点就发
                if gap_block and str(p.get("source") or "model") != "manual":
                    log(f"[关心计划] #{p['id']} 距上一条太近（{int(_gap_min)} 分钟内），等下一轮",
                        "INFO")
                    continue
                # v8：可选地过同一道"静默/模型决策"门槛
                if (not _ign) and bool(_live_cfg("care_plan_use_silence", False)):
                    ok_send, why = _checkin_should_send(
                        sk, {"event_desc": p.get("reason") or p.get("topic")})
                    if not ok_send:
                        _n, _w = _care_defer_note(p["id"])
                        _force = _care_defer_force(p["id"])
                        if not _force:
                            log(f"[关心计划] #{p['id']} 到点了但先不发：{why}"
                                f"（第 {_n} 次推迟，下一轮再看）", "INFO")
                            continue
                        log(f"[关心计划] #{p['id']} 到点了、{_force}，"
                            f"不再等（原本因为{why}），这次照发", "INFO")
                # 对方正在聊天时别按剧本硬插一句
                if CARE_PLAN_RESPECT_SILENCE and not _ign:
                    try:
                        last_user = mem.last_message_ts(sk, role="user")
                    except Exception:
                        last_user = 0
                    if last_user and (time.time() - last_user) < 8 * 60:
                        _n, _w = _care_defer_note(p["id"])
                        _force = _care_defer_force(p["id"])
                        if not _force:
                            log(f"[关心计划] #{p['id']} 对方正在聊，跳过"
                                f"（第 {_n} 次推迟，下一轮再看）", "INFO")
                            continue
                        log(f"[关心计划] #{p['id']} 对方一直在聊，但{_force}，"
                            f"不再等了，这次照发", "INFO")

                text = _care_plan_text(p)
                if not text:
                    _loop_log("关心计划", "计划 #" + str(p.get("id")) + " 的话术没生成出来（模型没返回），这轮不发", key="cp_gen|" + str(p.get("id")), gap=600)
                    continue
                # v1.6.3（QA P2-1）：同一会话、同一句话，60秒内只发一次
                _now_s = time.time()
                _prev = _CARE_SENT.get(sk) or (None, 0.0)
                if _prev[0] == text and (_now_s - float(_prev[1] or 0)) < _CARE_SENT_TTL:
                    log(f"[关心计划] #{p['id']}跟60秒内发过的内容一模一样，不重复发", "INFO")
                    mem.mark_plan_done(p["id"])
                    _care_defer_clear(p["id"])
                    continue
                try:
                    send_to_session(sk, text, remember=True)
                except Exception as e:
                    log(f"[关心计划] #{p['id']} 发送失败: {e}", "ERROR")
                    continue
                mem.mark_plan_done(p["id"])
                _CARE_SENT[sk] = (text, time.time())
                _care_defer_clear(p["id"])
                used += 1
                log(f"[关心计划] 已发 #{p['id']} -> {sk}：{text[:60]}", "INFO")
        except Exception as e:
            log(f"[关心计划] 循环异常: {type(e).__name__}: {e}", "ERROR")


