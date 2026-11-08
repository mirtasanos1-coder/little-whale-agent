# -*- coding: utf-8 -*-
"""小鲸鱼记忆模块 —— SQLite 存储

表结构：
  memories     长期记忆（关于某个人/某个群的稳定事实）
  chat_log     聊天流水（按人维度，可追溯"上次聊过什么"）
  messages     会话消息（按 session_key 聚合，用于短期上下文）
  summaries    滚动摘要（长对话压缩后的历史摘要）
  user_events  待办/提醒事件（"我明天9点要考试"这类有时效的计划）

线程安全：所有操作走 RLock；SQLite 开启 WAL 模式支持并发读。
兼容性：旧库自动迁移（chat_log.role 列、user_events 缺失等）。
"""
import os
import re
import sqlite3
import threading
import time

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")


def _data_root():
    """数据目录（与 agent.py 保持一致）"""
    import sys
    env_dir = os.environ.get("QQ_AGENT_DATA_DIR")
    if env_dir:
        return env_dir
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def memory_dir(root=None):
    """记忆工作目录：<数据目录>/memory/

    v7 起数据库、导出、备份都放这里，方便"整个文件夹拷走"就是完整记忆。
    """
    root = root or _data_root()
    d = os.path.join(root, "memory")
    try:
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, "exports"), exist_ok=True)
        os.makedirs(os.path.join(d, "backups"), exist_ok=True)
    except Exception:
        pass
    return d


def _migrate_old_db(new_path, old_path):
    """老版本把 memory.db 放在数据目录根上，v7 搬到 memory/ 下。

    策略：只在"新位置没有、老位置有"时复制一份（老文件保留不动，当备份）。
    """
    try:
        if os.path.exists(new_path) or not os.path.exists(old_path):
            return False
        import shutil
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        shutil.copy2(old_path, new_path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(old_path + suffix):
                try:
                    shutil.copy2(old_path + suffix, new_path + suffix)
                except Exception:
                    pass
        return True
    except Exception:
        return False


def _default_db_path():
    """数据库位置解析（与 agent.py 保持一致）

    v7：优先 <数据目录>/memory/memory.db；
        首次运行会把老的 <数据目录>/memory.db 复制过来（老文件保留）。
    也兼容 QQ_AGENT_DB_PATH 直接指定路径（自检用）。
    """
    env_db = os.environ.get("QQ_AGENT_DB_PATH")
    if env_db:
        return env_db
    root = _data_root()
    try:
        os.makedirs(root, exist_ok=True)
    except Exception:
        pass
    target = os.path.join(memory_dir(root), "memory.db")
    _migrate_old_db(target, os.path.join(root, "memory.db"))
    return target

# 事件状态
EVENT_PENDING = "pending"     # 等待触发
EVENT_DONE = "done"           # 已提醒过
EVENT_CANCELLED = "cancelled"  # 已取消


class Memory:
    def __init__(self, db_path=None):
        self.db_path = db_path or _default_db_path()
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_tables()
        # v1.8.0：知识图谱两张表跟着主库一起建（老库只加表，不动数据）
        try:
            import kg as _kgmod
            _kgmod.ensure_schema(self.conn)
        except Exception:
            pass
        # v1.8.12：用户情感画像表（老库只加表，不动数据）
        try:
            import emoprofile as _epmod
            _epmod.ensure_schema(self.conn)
        except Exception:
            pass

    # ==================== 建表 / 迁移 ====================
    def _init_tables(self):
        with self.lock:
            c = self.conn.cursor()
            # 长期记忆
            c.execute("""CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                person TEXT,
                fact TEXT NOT NULL,
                created_at REAL,
                last_used REAL,
                use_count INTEGER DEFAULT 0)""")
            # 聊天流水
            c.execute("""CREATE TABLE IF NOT EXISTS chat_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT,
                person TEXT,
                role TEXT,
                content TEXT,
                created_at REAL)""")
            # 会话消息
            c.execute("""CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL)""")
            # 滚动摘要
            c.execute("""CREATE TABLE IF NOT EXISTS summaries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                summary TEXT NOT NULL,
                ts REAL NOT NULL)""")
            # 待办事件（任务二核心表）
            c.execute("""CREATE TABLE IF NOT EXISTS user_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                event_desc TEXT NOT NULL,
                target_time REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL,
                session_key TEXT,
                fired_at REAL)""")
            # ---------- v4：永久记忆（只有绑定的账号才写这里） ----------
            # 与 memories 的区别：memories 是按 scope 分的"临时事实"，会随对话滚动被压缩；
            # perm_memory 是"绑定账号的终身档案"，带分类与重要度。
            # v5：加 status/supersedes/norm_key 三列 —— 支持"新事实顶掉旧事实"。
            #   status='active'     当前有效
            #   status='superseded' 已被更新的事实取代（保留可追溯，不参与注入）
            c.execute("""CREATE TABLE IF NOT EXISTS perm_memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                content TEXT NOT NULL,
                importance INTEGER DEFAULT 3,
                source TEXT,
                created_at REAL,
                last_used REAL,
                use_count INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                supersedes INTEGER DEFAULT 0,
                norm_key TEXT,
                updated_at REAL)""")
            # ---------- v4：日记 ----------
            c.execute("""CREATE TABLE IF NOT EXISTS diary(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                mood TEXT,
                msg_count INTEGER DEFAULT 0,
                created_at REAL)""")
            # ---------- v6：主动回复计划（场景感知） ----------
            # 与 user_events 的区别：
            #   user_events  = 显式的事实提醒（"明天9点考试"），到点问"考得怎么样"
            #   care_plans   = 模型自己判断"这会儿该关心一下"，到点主动开口，可手动增删
            c.execute("""CREATE TABLE IF NOT EXISTS care_plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                plan_at REAL NOT NULL,
                reason TEXT,
                topic TEXT,
                source TEXT NOT NULL DEFAULT 'model',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL,
                fired_at REAL,
                extra TEXT)""")
            # ---------- v6：说说评论（要看得到、回得了） ----------
            c.execute("""CREATE TABLE IF NOT EXISTS moment_comments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moment_tid TEXT,
                moment_text TEXT,
                commenter_id TEXT,
                commenter_name TEXT,
                content TEXT NOT NULL,
                reply TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT,
                created_at REAL,
                replied_at REAL)""")
            # ---------- v7：浏览别人动态（看过谁、怎么互动的） ----------
            # post_key 是去重键（有 tid 用 tid，没有就用 作者+内容hash），
            # 保证同一条动态不会被模型反复点赞/评论。
            c.execute("""CREATE TABLE IF NOT EXISTS feed_seen(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_key TEXT NOT NULL UNIQUE,
                source TEXT,
                author_id TEXT,
                author_name TEXT,
                content TEXT,
                images TEXT,
                action TEXT,
                reply TEXT,
                status TEXT NOT NULL DEFAULT 'seen',
                created_at REAL,
                updated_at REAL)""")

            # ---------- v8：好感度 ----------
            # 每个好友一行。score 越高关系越近（会换算成"关系等级"写进 system prompt）。
            # 只在 config 里 affinity_enabled=True 时才累积，默认关闭。
            c.execute("""CREATE TABLE IF NOT EXISTS affinity(
                user_id TEXT PRIMARY KEY,
                name TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                level TEXT NOT NULL DEFAULT 'stranger',
                msgs INTEGER NOT NULL DEFAULT 0,
                positive INTEGER NOT NULL DEFAULT 0,
                negative INTEGER NOT NULL DEFAULT 0,
                created_at REAL,
                updated_at REAL,
                last_decay_day TEXT)""")

            # v1.1.2 任务二：潜意识/自我反思（每天深夜整理出来的 3 条「心里话」）
            c.execute("""CREATE TABLE IF NOT EXISTS inner_thoughts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thought TEXT NOT NULL,
                day TEXT,
                created_at REAL)""")

            # v1.1.4 阶段二：思考过程独立存档（不进对话、不污染上下文，可复核）
            c.execute("""CREATE TABLE IF NOT EXISTS think_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT,
                user_text TEXT,
                thinking TEXT,
                reply TEXT,
                created_at REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_think_session ON think_log(session_key, id)")

            # v1.1.4 阶段二：情感决策归档（谁说的话、判成什么、权重、给了什么姿态）
            c.execute("""CREATE TABLE IF NOT EXISTS emotion_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT,
                user_text TEXT,
                emotion TEXT,
                weight INTEGER,
                suggestion TEXT,
                src TEXT,
                created_at REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_emolog_time ON emotion_log(created_at)")

            # v1.1.6 核心任务一：语义记忆（向量）表。
            # vec 用 float32 BLOB 存（numpy 直接能读），dim 用来分辨「是不是同一套模型的向量」。
            c.execute("""CREATE TABLE IF NOT EXISTS vec_memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT DEFAULT 'chat',
                session_key TEXT,
                user_id TEXT,
                content TEXT,
                vec BLOB,
                dim INTEGER DEFAULT 0,
                created_at REAL,
                meta TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_vec_kind ON vec_memory(kind)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_vec_time ON vec_memory(created_at)")

            # v1.1.4 阶段三：伴侣关系（只加表，不动 affinity；两套可各自开关）
            c.execute("""CREATE TABLE IF NOT EXISTS romance_state(
                user_id TEXT PRIMARY KEY,
                level TEXT DEFAULT 'friend',
                since REAL,
                note TEXT,
                updated_at REAL)""")

            c.execute("CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_log_scope ON chat_log(scope)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_key, id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sum_session ON summaries(session_key, id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ev_status ON user_events(status, target_time)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ev_user ON user_events(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_perm_user ON perm_memory(user_id, id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_diary_user ON diary(user_id, day)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_plan_due ON care_plans(status, plan_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_plan_session ON care_plans(session_key, id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cmt_status ON moment_comments(status, id)")
            # v7：feed_seen 的索引可以在这里建；moment_comments 的楼中楼索引要等
            #     _migrate() 补完 root_id 列之后再建（见 _migrate 末尾）
            c.execute("CREATE INDEX IF NOT EXISTS idx_feed_key ON feed_seen(post_key)")
            self.conn.commit()
            self._migrate()

    def _migrate(self):
        """旧库补列，缺什么补什么，绝不因为历史数据报错"""
        c = self.conn.cursor()
        migrations = [
            ("chat_log", "role", "ALTER TABLE chat_log ADD COLUMN role TEXT"),
            ("user_events", "session_key", "ALTER TABLE user_events ADD COLUMN session_key TEXT"),
            ("user_events", "fired_at", "ALTER TABLE user_events ADD COLUMN fired_at REAL"),
            ("user_events", "event_desc", "ALTER TABLE user_events ADD COLUMN event_desc TEXT"),
            ("user_events", "target_time", "ALTER TABLE user_events ADD COLUMN target_time REAL"),
            # v5：永久记忆支持"新事实顶掉旧事实"
            ("perm_memory", "status",
             "ALTER TABLE perm_memory ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
            ("perm_memory", "supersedes",
             "ALTER TABLE perm_memory ADD COLUMN supersedes INTEGER DEFAULT 0"),
            ("perm_memory", "norm_key", "ALTER TABLE perm_memory ADD COLUMN norm_key TEXT"),
            ("perm_memory", "updated_at", "ALTER TABLE perm_memory ADD COLUMN updated_at REAL"),
            # v6：用量明细（这一笔 token 是干什么花掉的）
            ("usage_log", "detail", "ALTER TABLE usage_log ADD COLUMN detail TEXT"),
            # v6：主动回复计划的补充字段
            ("care_plans", "topic", "ALTER TABLE care_plans ADD COLUMN topic TEXT"),
            ("care_plans", "extra", "ALTER TABLE care_plans ADD COLUMN extra TEXT"),
            # v7：评论支持"楼中楼"（别人回了模型的评论，模型要能接着回）
            ("moment_comments", "parent_id",
             "ALTER TABLE moment_comments ADD COLUMN parent_id INTEGER DEFAULT 0"),
            ("moment_comments", "root_id",
             "ALTER TABLE moment_comments ADD COLUMN root_id INTEGER DEFAULT 0"),
            ("moment_comments", "depth",
             "ALTER TABLE moment_comments ADD COLUMN depth INTEGER DEFAULT 0"),
            ("moment_comments", "direction",
             "ALTER TABLE moment_comments ADD COLUMN direction TEXT DEFAULT 'in'"),
            # v1.9.20：空间那边的评论 id（网页协议里的 commentid）。
            # 以前不存，回复时只能拿数据库自增 id 冒充 comment_id —— 网页端不认，
            # 而且同一句会被反复回（去重只能靠「同一个人 + 同样内容」比对）。
            ("moment_comments", "remote_id",
             "ALTER TABLE moment_comments ADD COLUMN remote_id TEXT DEFAULT ''"),
            # v1.1.4 阶段二/三：只加列、不改旧数据（默认值保证老记录语义不变）
            # unit：这条定时/计划的「时间单位」，老数据全是秒级时间戳，所以默认 'second'
            ("user_events", "unit",
             "ALTER TABLE user_events ADD COLUMN unit TEXT DEFAULT 'second'"),
            ("care_plans", "unit",
             "ALTER TABLE care_plans ADD COLUMN unit TEXT DEFAULT 'second'"),
            # v1.9.9：用户明确说的「几点叫我 / 提醒我」不能被静默门槛压住
            ("care_plans", "ignore_silence",
             "ALTER TABLE care_plans ADD COLUMN ignore_silence INTEGER DEFAULT 0"),
            ("care_plans", "priority",
             "ALTER TABLE care_plans ADD COLUMN priority INTEGER DEFAULT 0"),
            ("user_events", "ignore_silence",
             "ALTER TABLE user_events ADD COLUMN ignore_silence INTEGER DEFAULT 0"),
            ("user_events", "priority",
             "ALTER TABLE user_events ADD COLUMN priority INTEGER DEFAULT 0"),
            # feature：用量按「功能位」统计（配合 providers.FEATURES 的分工）
            ("usage_log", "feature",
             "ALTER TABLE usage_log ADD COLUMN feature TEXT"),
            # romance：好感度表上的伴侣标记（0=普通，1=伴侣）。旧行默认 0，行为不变
            ("affinity", "romance",
             "ALTER TABLE affinity ADD COLUMN romance INTEGER DEFAULT 0"),
            # msgs_at：这轮回复是在什么时候发生的（思考存档联动用）
            ("think_log", "msgs_at",
             "ALTER TABLE think_log ADD COLUMN msgs_at REAL"),
            # v1.9.10：日记改成「高频记忆快照」后，同一天会有多篇 —— 用 slot 区分
            ("diary", "slot",
             "ALTER TABLE diary ADD COLUMN slot TEXT DEFAULT ''"),
            # v1.1.6：关系是「自动跟着分数变」还是「用户手动钉死」
            ("romance_state", "auto",
             "ALTER TABLE romance_state ADD COLUMN auto INTEGER DEFAULT 1"),
        ]
        for table, col, sql in migrations:
            try:
                cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % table)]
                if cols and col not in cols:
                    c.execute(sql)
                    self.conn.commit()
            except sqlite3.Error:
                pass
        # 兼容旧版本曾用 desc 作为列名的情况
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(user_events)")]
            if "desc" in cols and "event_desc" not in cols:
                c.execute("ALTER TABLE user_events RENAME COLUMN desc TO event_desc")
                self.conn.commit()
        except sqlite3.Error:
            pass
        # v7：楼中楼索引（必须在补列之后建，否则 no such column: root_id）
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_cmt_thread "
                      "ON moment_comments(root_id, id)")
            self.conn.commit()
        except sqlite3.Error:
            pass

    # ==================== 长期记忆 ====================
    def add_fact(self, scope, person, fact):
        """新增一条记忆，重复内容自动跳过。返回是否新增。"""
        fact = (fact or "").strip()
        if not fact:
            return False
        if len(fact) > 200:
            fact = fact[:200]
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT 1 FROM memories WHERE scope=? AND fact=? LIMIT 1", (scope, fact))
            if c.fetchone():
                return False
            c.execute(
                "INSERT INTO memories(scope,person,fact,created_at,last_used,use_count) "
                "VALUES(?,?,?,?,?,0)",
                (scope, person, fact, time.time(), time.time()))
            # 每个 scope 最多 60 条，超出删最旧
            c.execute("""DELETE FROM memories WHERE scope=? AND id NOT IN (
                         SELECT id FROM memories WHERE scope=?
                         ORDER BY last_used DESC LIMIT 60)""", (scope, scope))
            self.conn.commit()
            return True

    def get_facts(self, scope, limit=12):
        """取一个会话的记忆，按最近使用+次数排序。同时刷新 last_used。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, person, fact FROM memories WHERE scope=?
                         ORDER BY last_used DESC, use_count DESC LIMIT ?""",
                      (scope, limit))
            rows = c.fetchall()
            if rows:
                ids = [r[0] for r in rows]
                c.executemany(
                    "UPDATE memories SET last_used=?, use_count=use_count+1 WHERE id=?",
                    [(time.time(), i) for i in ids])
                self.conn.commit()
            return [(r[1], r[2]) for r in rows]

    def clear_scope(self, scope):
        """清空某会话的全部记忆（"忘掉"指令）。返回删除条数。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT COUNT(*) FROM memories WHERE scope=?", (scope,))
            n = c.fetchone()[0]
            c.execute("DELETE FROM memories WHERE scope=?", (scope,))
            self.conn.commit()
            return n

    def count(self, scope=None):
        with self.lock:
            c = self.conn.cursor()
            if scope:
                c.execute("SELECT COUNT(*) FROM memories WHERE scope=?", (scope,))
            else:
                c.execute("SELECT COUNT(*) FROM memories")
            return c.fetchone()[0]

    # ==================== 会话消息 ====================
    def add_message(self, session_key, role, content):
        """把一轮对话写入 messages 表（保留完整历史，不截断）"""
        if not content:
            return
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT INTO messages (session_key, role, content, ts) VALUES (?, ?, ?, ?)",
                (session_key, role, str(content), time.time()))
            self.conn.commit()

    def get_recent_messages(self, session_key, turns=8):
        """取最近 N 轮对话（最多 2N 条），正序返回 [{"role","content","ts"}, ...]

        v1.3.2（QA BUG-09）：以前不带 ts，调用方按三元组解包会抛
        `ValueError: not enough values to unpack`，被外层 except 一吞，
        「6 小时内刚聊过就不打招呼」这条守卫就永久失效了。现在把 ts 带上。
        """
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "SELECT role, content, ts FROM messages WHERE session_key=? "
                "ORDER BY id DESC LIMIT ?",
                (session_key, max(1, int(turns)) * 2))
            rows = c.fetchall()
        rows.reverse()
        return [{"role": r[0], "content": r[1], "ts": r[2]} for r in rows]

    def recent_rows(self, session_key, limit=200):
        """某个会话的消息（带 ts，记忆页筛选用），按时间正序。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT session_key, role, content, ts FROM messages
                         WHERE session_key=? ORDER BY id DESC LIMIT ?""",
                      (str(session_key), int(limit)))
            rows = c.fetchall()
        rows.reverse()
        return [{"session_key": r[0], "role": r[1], "content": r[2], "ts": r[3]}
                for r in rows]

    def count_messages(self, session_key):
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT COUNT(*) FROM messages WHERE session_key=?", (session_key,))
            return c.fetchone()[0]

    def list_sessions(self, limit=40):
        """最近有消息的会话（记忆页选会话用）。

        v1.1.2：以前临时记忆页只能看「当前/最后一个会话」，群里的聊天根本翻不到。
        现在顺带给出：类型（私聊/群聊）、号码、中文标签、最近一条内容、是否已绑定 ——
        前端下拉直接显示「群 123456（已绑定）· 最近：xxx」，不用自己猜。
        """
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT session_key, MAX(ts) AS last_ts, COUNT(*) AS n
                         FROM messages GROUP BY session_key
                         ORDER BY last_ts DESC LIMIT ?""", (int(limit),))
            rows = c.fetchall()
            out = []
            for key, last_ts, n in rows:
                last_text = ""
                try:
                    c.execute("SELECT content FROM messages WHERE session_key=? "
                              "ORDER BY id DESC LIMIT 1", (key,))
                    r = c.fetchone()
                    last_text = str(r[0] or "")[:80] if r else ""
                except Exception:
                    last_text = ""
                out.append({
                    "key": key,
                    "kind": "group" if key.startswith("group_") else (
                        "private" if key.startswith("private_") else "other"),
                    "num": key.split("_", 1)[1] if "_" in key else "",
                    "label": key,
                    "last_ts": last_ts or 0,
                    "count": n,
                    "last_text": last_text,
                    "bound": False,
                })
        # 绑定关系（群白名单 / 绑定账号）在 config 里，单独读一次，避免锁着库去读盘
        try:
            import config as cfg_mod
            cfg = cfg_mod.load()
            wl = {str(x).strip() for x in (cfg.get("group_whitelist") or [])}
            acc = {str(x).strip() for x in (cfg.get("diary_bound_accounts") or [])}
        except Exception:
            wl, acc = set(), set()
        for it in out:
            num = it.get("num") or ""
            bound = (num in wl) if it["kind"] == "group" else (num in acc)
            it["bound"] = bool(bound)
            if it["kind"] == "group":
                it["label"] = f"群 {num}"
            elif it["kind"] == "private":
                it["label"] = f"私聊 {num}"
            else:
                it["label"] = it["key"]
            if it["bound"]:
                it["label"] += "（已绑定）"
        return out

    def get_earliest_messages(self, session_key, limit):
        """按时间从旧到新取最早的 limit 条，返回 [(role, content), ...]"""
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "SELECT role, content FROM messages WHERE session_key=? ORDER BY id ASC LIMIT ?",
                (session_key, int(limit)))
            return c.fetchall()

    def last_message_ts(self, session_key=None, role=None):
        """最后一次消息时间戳。

        role="user" 时只看用户说的话（查岗判断"用户多久没开口"用这个）。
        不传 session_key 则取全库最新。
        """
        with self.lock:
            c = self.conn.cursor()
            sql = "SELECT MAX(ts) FROM messages WHERE 1=1"
            args = []
            if session_key:
                sql += " AND session_key=?"
                args.append(session_key)
            if role:
                sql += " AND role=?"
                args.append(role)
            c.execute(sql, args)
            row = c.fetchone()
            return float(row[0]) if row and row[0] else 0.0

    def recent_sessions(self, limit=20):
        """最近活跃的会话列表：[(session_key, last_ts, msg_count), ...]"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT session_key, MAX(ts) AS t, COUNT(*) AS n
                         FROM messages GROUP BY session_key
                         ORDER BY t DESC LIMIT ?""", (limit,))
            return [(r[0], float(r[1] or 0), int(r[2] or 0)) for r in c.fetchall()]

    # ==================== 摘要 ====================
    def save_summary(self, session_key, summary):
        if not summary:
            return
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT INTO summaries (session_key, summary, ts) VALUES (?, ?, ?)",
                (session_key, str(summary), time.time()))
            self.conn.commit()

    def get_latest_summary(self, session_key):
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "SELECT summary FROM summaries WHERE session_key=? ORDER BY id DESC LIMIT 1",
                (session_key,))
            row = c.fetchone()
        return row[0] if row else None

    # ==================== 思考过程存档（v1.1.4 阶段二）====================
    def log_thinking(self, session_key, user_text, thinking, reply):
        """<thinking> 段单独存，不进对话历史，只供复核。"""
        think = str(thinking or "").strip()
        if not think:
            return 0
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT INTO think_log (session_key, user_text, thinking, reply, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(session_key or ""), str(user_text or "")[:500], think[:2000],
                 str(reply or "")[:2000], time.time()))
            self.conn.commit()
            return int(c.lastrowid or 0)

    def recent_thinking(self, limit=20, session_key=None):
        with self.lock:
            c = self.conn.cursor()
            if session_key:
                c.execute("SELECT user_text, thinking, reply, created_at FROM think_log"
                          " WHERE session_key=? ORDER BY id DESC LIMIT ?",
                          (str(session_key), int(limit)))
            else:
                c.execute("SELECT user_text, thinking, reply, created_at FROM think_log"
                          " ORDER BY id DESC LIMIT ?", (int(limit),))
            rows = c.fetchall()
        return [{"user_text": r[0], "thinking": r[1], "reply": r[2], "ts": r[3]}
                for r in rows]

    # ==================== 情感决策归档（v1.1.5.1）====================
    # 为什么必须落库：Agent 是 app_ui 用 subprocess 另起的进程，情感判断发生在
    # Agent 进程里。之前只写模块内存的 deque，控制台（另一个进程）永远读到空，
    # 用户看到的就是「聊了很多轮，一条判断都没有」。现在两边都写。
    def log_emotion(self, session_key, user_text, emotion, weight, suggestion="",
                    src="local", action=""):
        try:
            w = int(round(float(weight or 0)))
        except Exception:
            w = 0
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT INTO emotion_log (session_key, user_text, emotion, weight,"
                " suggestion, src, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(session_key or "")[:32], str(user_text or "")[:300],
                 str(emotion or "")[:24], max(0, min(100, w)),
                 str(suggestion or "")[:120], str(src or "local")[:12],
                 time.time()))
            self.conn.commit()
            return int(c.lastrowid or 0)

    def list_emotion_log(self, limit=60):
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT session_key, user_text, emotion, weight, suggestion,"
                      " src, created_at FROM emotion_log ORDER BY id DESC LIMIT ?",
                      (int(limit),))
            rows = c.fetchall()
        return [{"scope": r[0], "text": r[1], "emotion": r[2], "weight": r[3],
                 "suggestion": r[4], "src": r[5], "ts": r[6],
                 "time": time.strftime("%H:%M:%S", time.localtime(r[6] or 0))}
                for r in rows]

    def emotion_stats(self, days=7):
        """近 N 天的判断统计（按 created_at 截）。"""
        since = time.time() - max(1, int(days)) * 86400
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT COUNT(*), SUM(CASE WHEN src='model' THEN 1 ELSE 0 END),"
                      " SUM(CASE WHEN weight>=80 THEN 1 ELSE 0 END), AVG(weight)"
                      " FROM emotion_log WHERE created_at>=?", (since,))
            r = c.fetchone() or (0, 0, 0, 0)
        total = int(r[0] or 0)
        by_model = int(r[1] or 0)
        return {"total": total, "by_model": by_model, "by_local": total - by_model,
                "high": int(r[2] or 0), "avg_weight": int(r[3] or 0), "days": int(days)}

    def clear_emotion_log(self):
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM emotion_log")
            self.conn.commit()
            return int(c.rowcount or 0)

    # ==================== 潜意识（v1.1.2 任务二）====================
    def add_thought(self, thought, day=""):
        """写一条「自我反思」。day 用 YYYY-MM-DD，方便判断今天做没做过。"""
        text = str(thought or "").strip()
        if not text:
            return 0
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT INTO inner_thoughts (thought, day, created_at) VALUES (?, ?, ?)",
                (text[:500], str(day or ""), time.time()))
            self.conn.commit()
            return int(c.lastrowid or 0)

    def recent_thoughts(self, limit=3):
        """最近的几条反思（新的在前）。表不存在/空表都安全返回 []。"""
        try:
            with self.lock:
                c = self.conn.cursor()
                c.execute("SELECT thought FROM inner_thoughts ORDER BY id DESC LIMIT ?",
                          (max(1, int(limit or 3)),))
                rows = c.fetchall()
            return [r[0] for r in rows if r and r[0]]
        except Exception:
            return []

    def list_thoughts(self, limit=60):
        """v1.1.6：自省页用 —— 带日期与时间的完整列表（新的在前）。"""
        try:
            with self.lock:
                c = self.conn.cursor()
                c.execute("SELECT thought, day, created_at FROM inner_thoughts "
                          "ORDER BY id DESC LIMIT ?", (max(1, int(limit or 60)),))
                rows = c.fetchall()
            return [{"thought": r[0] or "", "day": r[1] or "",
                     "time": float(r[2] or 0)} for r in rows]
        except Exception:
            return []

    def has_thought_for_day(self, day):
        try:
            with self.lock:
                c = self.conn.cursor()
                c.execute("SELECT 1 FROM inner_thoughts WHERE day=? LIMIT 1", (str(day),))
                return c.fetchone() is not None
        except Exception:
            return False

    def messages_since(self, ts, limit=200):
        """某时间点之后的所有消息（跨会话），潜意识反思用。返回 [(key, role, content)]。"""
        try:
            with self.lock:
                c = self.conn.cursor()
                c.execute("SELECT session_key, role, content FROM messages "
                          "WHERE ts >= ? ORDER BY id ASC LIMIT ?",
                          (float(ts or 0), max(1, int(limit or 200))))
                rows = c.fetchall()
            return [(r[0], r[1], r[2]) for r in rows]
        except Exception:
            return []

    def old_perm_memories(self, older_than_days=180, limit=20):
        """很久没被用到、也没更新过的永久记忆（记忆衰退用）。"""
        try:
            cutoff = time.time() - max(1, int(older_than_days or 180)) * 86400.0
            with self.lock:
                c = self.conn.cursor()
                c.execute("SELECT id, user_id, content FROM perm_memory "
                          "WHERE created_at < ? ORDER BY id ASC LIMIT ?",
                          (cutoff, max(1, int(limit or 20))))
                return c.fetchall()
        except Exception:
            return []

    def update_perm_content(self, mem_id, content):
        try:
            with self.lock:
                c = self.conn.cursor()
                c.execute("UPDATE perm_memory SET content=? WHERE id=?",
                          (str(content)[:400], int(mem_id)))
                self.conn.commit()
            return True
        except Exception:
            return False

    # ==================== 聊天流水 ====================
    def log_message(self, scope, person, role, content):
        """写一条流水。注意：4 参数签名（scope, person, role, content）。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT INTO chat_log(scope,person,role,content,created_at) "
                "VALUES(?,?,?,?,?)",
                (scope, person, role, str(content), time.time()))
            # 全库上限 2 万条，超出删最旧
            c.execute("DELETE FROM chat_log WHERE id NOT IN "
                      "(SELECT id FROM chat_log ORDER BY id DESC LIMIT 20000)")
            self.conn.commit()

    def recent_chat(self, scope, limit=10):
        """取某个会话最近的流水（正序返回）[(person, role, content), ...]"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT person, role, content FROM chat_log
                         WHERE scope=? ORDER BY id DESC LIMIT ?""",
                      (scope, limit))
            rows = c.fetchall()
        return list(reversed(rows))

    # ==================== 待办事件（任务二） ====================
    def add_event(self, user_id, event_desc, target_time, session_key=None, dedup=True, ignore_silence=0, priority=0):
        """新增一条待办事件。

        user_id      : 私聊 QQ 号 / 群号（字符串化后存储）
        event_desc   : 事件描述，如"明天9点考试"
        target_time  : 事件发生的 unix 时间戳
        session_key  : 归属会话，用于查岗时决定发到哪里
        dedup        : 同用户 + 同描述 + 同一小时内不重复插入
        返回新事件 id，重复则返回 0。
        """
        event_desc = (event_desc or "").strip()
        if not event_desc or not target_time:
            return 0
        event_desc = event_desc[:200]
        ignore_silence = 1 if ignore_silence else 0
        priority = 1 if priority else 0
        uid = str(user_id)
        with self.lock:
            c = self.conn.cursor()
            if dedup:
                c.execute("""SELECT id FROM user_events
                             WHERE user_id=? AND event_desc=? AND status='pending'
                               AND ABS(target_time-?) < 3600 LIMIT 1""",
                          (uid, event_desc, float(target_time)))
                if c.fetchone():
                    return 0
            c.execute("""INSERT INTO user_events
                      (user_id, event_desc, target_time, status, created_at, session_key,
                       ignore_silence, priority)
                      VALUES (?,?,?,?,?,?,?,?)""",
                    (uid, event_desc, float(target_time), EVENT_PENDING,
                     time.time(), session_key, ignore_silence, priority))
            self.conn.commit()
            return int(c.lastrowid or 0)
    def get_due_events(self, now=None, grace_sec=0):
        """取所有已到期、仍待触发的 pending 事件。

        返回 [{"id","user_id","event_desc","target_time","session_key"}, ...]
        """
        now = time.time() if now is None else float(now)
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, user_id, event_desc, target_time, session_key,
                       COALESCE(ignore_silence,0), COALESCE(priority,0)
                     FROM user_events
                     WHERE status=? AND target_time <= ?
                     ORDER BY target_time ASC""",
                    (EVENT_PENDING, now - grace_sec))
            rows = c.fetchall()
        return [{"id": r[0], "user_id": r[1], "event_desc": r[2],
                 "target_time": float(r[3] or 0), "session_key": r[4],
                 "ignore_silence": int(r[5] or 0), "priority": int(r[6] or 0)} for r in rows]

    # 兼容旧命名
    def get_pending_events(self, now=None):
        return self.get_due_events(now=now)

    def list_events(self, limit=50, status=None):
        """列出事件（供 UI 展示），按目标时间倒序。"""
        with self.lock:
            c = self.conn.cursor()
            if status:
                c.execute("""SELECT id, user_id, event_desc, target_time, status,
                                    created_at, session_key
                             FROM user_events WHERE status=?
                             ORDER BY target_time DESC LIMIT ?""", (status, limit))
            else:
                c.execute("""SELECT id, user_id, event_desc, target_time, status,
                                    created_at, session_key
                             FROM user_events
                             ORDER BY target_time DESC LIMIT ?""", (limit,))
            rows = c.fetchall()
        return [{"id": r[0], "user_id": r[1], "event_desc": r[2],
                 "target_time": float(r[3] or 0), "status": r[4],
                 "created_at": float(r[5] or 0), "session_key": r[6]} for r in rows]

    def update_event(self, event_id, event_desc=None, target_time=None):
        """v1.1.1：改待办内容 / 时间（只动传进来的字段）。"""
        sets, args = [], []
        if event_desc is not None:
            desc = str(event_desc).strip()[:200]
            if not desc:
                return False
            sets.append("event_desc=?")
            args.append(desc)
        if target_time is not None:
            sets.append("target_time=?")
            args.append(float(target_time))
        if not sets:
            return False
        args.append(int(event_id))
        with self.lock:
            c = self.conn.cursor()
            c.execute(f"UPDATE user_events SET {', '.join(sets)} WHERE id=?", args)
            self.conn.commit()
            return c.rowcount > 0

    def delete_event(self, event_id):
        """v1.1.1：彻底删掉一条待办。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM user_events WHERE id=?", (int(event_id),))
            self.conn.commit()
            return c.rowcount > 0

    def mark_event_done(self, event_id):
        """把事件标记为已完成（已提醒过），并记录触发时间。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("UPDATE user_events SET status=?, fired_at=? WHERE id=?",
                      (EVENT_DONE, time.time(), int(event_id)))
            self.conn.commit()
            return c.rowcount > 0

    def cancel_event(self, event_id):
        with self.lock:
            c = self.conn.cursor()
            c.execute("UPDATE user_events SET status=? WHERE id=?",
                      (EVENT_CANCELLED, int(event_id)))
            self.conn.commit()
            return c.rowcount > 0

    def count_events(self, status=None):
        with self.lock:
            c = self.conn.cursor()
            if status:
                c.execute("SELECT COUNT(*) FROM user_events WHERE status=?", (status,))
            else:
                c.execute("SELECT COUNT(*) FROM user_events")
            return c.fetchone()[0]

    # ==================== v6：主动回复计划 ====================
    PLAN_PENDING = "pending"
    PLAN_DONE = "done"
    PLAN_CANCELLED = "cancelled"

    def add_plan(self, user_id, session_key, plan_at, reason="", topic="",
                 source="model", extra="", dedup_min=10, ignore_silence=0,
                 priority=0):
        """排一条"到点主动开口"的计划。

        返回 (id, is_new)。同会话 + 同 reason + 时间相近（dedup_min 分钟内）视为重复，
        返回已存在的 id 且 is_new=False —— 避免模型每次对话都排一条一模一样的。
        """
        try:
            plan_at = float(plan_at)
        except (TypeError, ValueError):
            return 0, False
        user_id = str(user_id or "").strip()
        session_key = str(session_key or "").strip()
        if not user_id or not session_key:
            return 0, False
        reason = (reason or "").strip()[:200]
        topic = (topic or "").strip()[:300]
        extra = (extra or "").strip()[:400]
        ignore_silence = 1 if ignore_silence else 0
        priority = 1 if priority else 0
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id FROM care_plans
                         WHERE session_key=? AND status=? AND reason=?
                           AND ABS(plan_at - ?) <= ?
                         LIMIT 1""",
                      (session_key, self.PLAN_PENDING, reason, plan_at,
                       max(60.0, float(dedup_min) * 60)))
            row = c.fetchone()
            if row:
                return int(row[0]), False
            c.execute("""INSERT INTO care_plans
                      (user_id, session_key, plan_at, reason, topic, source,
                       status, created_at, fired_at, extra, ignore_silence, priority)
                      VALUES(?,?,?,?,?,?,?,?,0,?,?,?)""",
                    (user_id, session_key, plan_at, reason, topic, source,
                     self.PLAN_PENDING, time.time(), extra, ignore_silence, priority))
            self.conn.commit()
            return int(c.lastrowid), True

    def get_due_plans(self, now=None, grace_sec=0):
        """取所有到点的待触发计划（按计划时间正序）。"""
        now = float(now if now is not None else time.time())
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, user_id, session_key, plan_at, reason, topic,
                       source, extra, COALESCE(ignore_silence,0), COALESCE(priority,0)
                     FROM care_plans
                     WHERE status=? AND plan_at <= ?
                     ORDER BY plan_at ASC LIMIT 20""",
                    (self.PLAN_PENDING, now + float(grace_sec or 0)))
            rows = c.fetchall()
        return [{"id": r[0], "user_id": r[1], "session_key": r[2],
                 "plan_at": float(r[3] or 0), "reason": r[4] or "",
                 "topic": r[5] or "", "source": r[6] or "model",
                 "extra": r[7] or "", "ignore_silence": int(r[8] or 0),
                 "priority": int(r[9] or 0)} for r in rows]

    def list_plans(self, limit=50, status=None, session_key=None):
        """列出计划（供界面展示），待触发的按时间正序在前。"""
        with self.lock:
            c = self.conn.cursor()
            sql = ("SELECT id, user_id, session_key, plan_at, reason, topic, "
                   "source, status, created_at, fired_at FROM care_plans")
            args = []
            where = []
            if status:
                where.append("status=?")
                args.append(status)
            if session_key:
                where.append("session_key=?")
                args.append(session_key)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += (" ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,"
                    " plan_at ASC LIMIT ?")
            args.append(int(limit))
            c.execute(sql, args)
            rows = c.fetchall()
        return [{"id": r[0], "user_id": r[1], "session_key": r[2],
                 "plan_at": float(r[3] or 0), "reason": r[4] or "",
                 "topic": r[5] or "", "source": r[6] or "model",
                 "status": r[7] or "pending", "created_at": float(r[8] or 0),
                 "fired_at": float(r[9] or 0)} for r in rows]

    def mark_plan_done(self, plan_id, status=None):
        with self.lock:
            c = self.conn.cursor()
            c.execute("UPDATE care_plans SET status=?, fired_at=? WHERE id=?",
                      (status or self.PLAN_DONE, time.time(), int(plan_id)))
            self.conn.commit()
            return c.rowcount > 0

    def cancel_plan(self, plan_id):
        return self.mark_plan_done(plan_id, self.PLAN_CANCELLED)

    def update_plan(self, plan_id, plan_at=None, reason=None, topic=None):
        """改一条计划的时间/理由（界面上手动编辑用）。"""
        sets, args = [], []
        if plan_at is not None:
            try:
                sets.append("plan_at=?")
                args.append(float(plan_at))
            except (TypeError, ValueError):
                return False, "时间格式不对"
        if reason is not None:
            sets.append("reason=?")
            args.append(str(reason).strip()[:200])
        if topic is not None:
            sets.append("topic=?")
            args.append(str(topic).strip()[:300])
        if not sets:
            return False, "没有要改的字段"
        args.append(int(plan_id))
        with self.lock:
            c = self.conn.cursor()
            c.execute("UPDATE care_plans SET " + ", ".join(sets) + " WHERE id=?", args)
            self.conn.commit()
            return (c.rowcount > 0), ("已保存" if c.rowcount else "没找到这条计划")

    def delete_plan(self, plan_id):
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM care_plans WHERE id=?", (int(plan_id),))
            self.conn.commit()
            return c.rowcount > 0

    def count_plans_today(self, session_key=None, day=None):
        """某个会话今天已经主动开口几次（防骚扰配额）。"""
        day = day or time.strftime("%Y-%m-%d")
        start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        end = start + 86400
        with self.lock:
            c = self.conn.cursor()
            if session_key:
                c.execute("""SELECT COUNT(*) FROM care_plans
                             WHERE session_key=? AND status=? AND fired_at>=? AND fired_at<?""",
                          (session_key, self.PLAN_DONE, start, end))
            else:
                c.execute("""SELECT COUNT(*) FROM care_plans
                             WHERE status=? AND fired_at>=? AND fired_at<?""",
                          (self.PLAN_DONE, start, end))
            return c.fetchone()[0]

    def last_plan_ts(self, session_key=None):
        """上一次真的主动开口的时间戳（0 = 从没说过）。"""
        with self.lock:
            c = self.conn.cursor()
            if session_key:
                c.execute("""SELECT MAX(fired_at) FROM care_plans
                             WHERE session_key=? AND status=?""",
                          (session_key, self.PLAN_DONE))
            else:
                c.execute("SELECT MAX(fired_at) FROM care_plans WHERE status=?",
                          (self.PLAN_DONE,))
            v = c.fetchone()[0]
            return float(v or 0)

    def count_plans(self, status=None):
        with self.lock:
            c = self.conn.cursor()
            if status:
                c.execute("SELECT COUNT(*) FROM care_plans WHERE status=?", (status,))
            else:
                c.execute("SELECT COUNT(*) FROM care_plans")
            return c.fetchone()[0]

    # ==================== v6：说说评论 ====================
    COMMENT_PENDING = "pending"
    COMMENT_REPLIED = "replied"
    COMMENT_SKIPPED = "skipped"

    def add_comment(self, content, commenter_id="", commenter_name="",
                    moment_tid="", moment_text="", source="napcat",
                    parent_id=0, root_id=0, depth=0, direction="in", remote_id=""):
        """收一条说说评论。同一条说说 + 同一个人 + 同样的内容只收一次。
        
        v7：支持楼中楼。别人回复了"模型写的那条评论"时，root_id 指向最初那条评论，
        parent_id 指向被回复的那条，direction="in"；模型自己发出去的记 direction="out"。
        
        v1.9.9：评论流里会把她自己发出去的评论也读回来（自己的号 id 是空的，
        按 (tid, 人, 内容) 去重拦不住），于是自己回自己无限套娃。现在只要同一条
        说说里已经有一条内容完全相同的 out 记录，就认定这是回声，不入队。

        v1.9.20：remote_id 是空间那边的评论 id。有它就用它去重 —— 比「人 + 内容」
        准得多：同一个人隔天说同样的话（「哈哈哈」）本来该算新评论，而旧逻辑会把
        它当成重复丢掉；反过来，内容被网页端截断/改写时旧逻辑又拦不住。
        """
        content = (content or "").strip()
        if not content:
            return 0, False
        remote_id = str(remote_id or "").strip()
        with self.lock:
            c = self.conn.cursor()
            if (direction or "in") != "out":
                c.execute("""SELECT id FROM moment_comments
                           WHERE COALESCE(moment_tid,'')=? AND content=? AND direction='out'
                           LIMIT 1""", (moment_tid or "", content))
                if c.fetchone():
                    return 0, False
            if remote_id:
                c.execute("""SELECT id FROM moment_comments
                           WHERE COALESCE(moment_tid,'')=? AND remote_id=?
                           LIMIT 1""", (moment_tid or "", remote_id))
                row = c.fetchone()
                if row:
                    return int(row[0]), False
            c.execute("""SELECT id FROM moment_comments
                           WHERE COALESCE(moment_tid,'')=? AND COALESCE(commenter_id,'')=?
                             AND content=? LIMIT 1""",
                          (moment_tid or "", str(commenter_id or ""), content))
            row = c.fetchone()
            if row:
                return int(row[0]), False
            c.execute("""INSERT INTO moment_comments
                          (moment_tid, moment_text, commenter_id, commenter_name,
                           content, reply, status, source, created_at, replied_at,
                           parent_id, root_id, depth, direction, remote_id)
                          VALUES(?,?,?,?,?,'',?,?,?,0,?,?,?,?,?)""",
                         (moment_tid or "", (moment_text or "")[:1000],
                          str(commenter_id or ""), commenter_name or "",
                          content[:800], self.COMMENT_PENDING, source, time.time(),
                          int(parent_id or 0), int(root_id or 0), int(depth or 0),
                          direction or "in", remote_id))
            self.conn.commit()
            return int(c.lastrowid), True

    def thread_last_direction(self, root_id=0):
        """v1.9.9：某个评论线索里最后一条是谁发的（in / out）。
        
        用来判断「这一串是不是已经轮到我说话了」——上一句已经是她说的话，
        就不该再往下接，否则就是自己跟自己聊。
        """
        rid = int(root_id or 0)
        if rid <= 0:
            return ""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT COALESCE(direction,'in') FROM moment_comments
                       WHERE id=? OR COALESCE(root_id,0)=?
                       ORDER BY id DESC LIMIT 1""", (rid, rid))
            row = c.fetchone()
        return (str(row[0] or "in") if row else "")
        
    def get_pending_comments(self, limit=10):
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, moment_tid, moment_text, commenter_id,
                                commenter_name, content, source,
                                COALESCE(parent_id,0), COALESCE(root_id,0), COALESCE(depth,0),
                                COALESCE(remote_id,'')
                         FROM moment_comments WHERE status=?
                         ORDER BY id ASC LIMIT ?""",
                      (self.COMMENT_PENDING, int(limit)))
            rows = c.fetchall()
        return [{"id": r[0], "moment_tid": r[1] or "", "moment_text": r[2] or "",
                 "commenter_id": r[3] or "", "commenter_name": r[4] or "",
                 "content": r[5] or "", "source": r[6] or "",
                 "parent_id": int(r[7] or 0), "root_id": int(r[8] or 0),
                 "depth": int(r[9] or 0), "remote_id": r[10] or ""} for r in rows]

    def get_comment(self, comment_id):
        """v1.9.20：按 id 取一条评论的当前状态（发送前要知道它是不是已经被回过）。"""
        try:
            cid = int(comment_id)
        except Exception:
            return {}
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, moment_tid, moment_text, commenter_id,
                                commenter_name, content, reply, status, source,
                                created_at, replied_at, COALESCE(remote_id,''),
                                COALESCE(root_id,0)
                         FROM moment_comments WHERE id=?""", (cid,))
            r = c.fetchone()
        if not r:
            return {}
        return {"id": r[0], "moment_tid": r[1] or "", "moment_text": r[2] or "",
                "commenter_id": r[3] or "", "commenter_name": r[4] or "",
                "content": r[5] or "", "reply": r[6] or "", "status": r[7] or "",
                "source": r[8] or "", "created_at": float(r[9] or 0),
                "replied_at": float(r[10] or 0), "remote_id": r[11] or "",
                "root_id": int(r[12] or 0)}

    def mark_comment(self, comment_id, status=None, reply=None):
        with self.lock:
            c = self.conn.cursor()
            c.execute("UPDATE moment_comments SET status=?, reply=COALESCE(?, reply), "
                      "replied_at=? WHERE id=?",
                      (status or self.COMMENT_REPLIED, reply,
                       time.time(), int(comment_id)))
            self.conn.commit()
            return c.rowcount > 0

    def list_comments(self, limit=50, status=None):
        with self.lock:
            c = self.conn.cursor()
            if status:
                c.execute("""SELECT id, moment_tid, commenter_name, commenter_id,
                                    content, reply, status, created_at, replied_at,
                                    COALESCE(root_id,0), COALESCE(parent_id,0),
                                    COALESCE(direction,'in')
                             FROM moment_comments WHERE status=?
                             ORDER BY id DESC LIMIT ?""", (status, int(limit)))
            else:
                c.execute("""SELECT id, moment_tid, commenter_name, commenter_id,
                                    content, reply, status, created_at, replied_at,
                                    COALESCE(root_id,0), COALESCE(parent_id,0),
                                    COALESCE(direction,'in')
                             FROM moment_comments ORDER BY id DESC LIMIT ?""",
                          (int(limit),))
            rows = c.fetchall()
        return [{"id": r[0], "moment_tid": r[1] or "", "commenter_name": r[2] or "",
                 "commenter_id": r[3] or "", "content": r[4] or "",
                 "reply": r[5] or "", "status": r[6] or "pending",
                 "created_at": float(r[7] or 0), "replied_at": float(r[8] or 0),
                 "root_id": int(r[9] or 0), "parent_id": int(r[10] or 0),
                 "direction": r[11] or "in"}
                for r in rows]

    def add_our_comment(self, content, moment_tid="", parent_id=0, root_id=0, depth=0,
                        commenter_name="__self__", remote_id=""):
        """记一条"模型自己发出去的评论"，用于楼中楼追踪（direction='out'）。

        这样别人再回复时，我们能把整条线索串起来。
        remote_id 是空间那边的 commentid（V1.9.20 起网页协议能拿到），
        有它就能和网页端读回来的评论精确对上，不再只靠内容比对。
        """
        content = (content or "").strip()
        if not content:
            return 0
        with self.lock:
            c = self.conn.cursor()
            c.execute("""INSERT INTO moment_comments
                         (moment_tid, moment_text, commenter_id, commenter_name,
                          content, reply, status, source, created_at, replied_at,
                          parent_id, root_id, depth, direction, remote_id)
                         VALUES(?,?,'',?,?,'',?, 'agent', ?,?,?,?,?, 'out', ?)""",
                      (moment_tid or "", "", commenter_name,
                       content[:800], self.COMMENT_REPLIED, time.time(),
                       time.time(), int(parent_id or 0), int(root_id or 0),
                       int(depth or 0), str(remote_id or "")))
            self.conn.commit()
            return int(c.lastrowid)

    def find_comment_by_remote(self, moment_tid, remote_id):
        """按空间那边的 commentid 找库内那一条。找不到返回 {}。

        V1.9.20 用：网页协议抓到的评论和库里那条要对上，才能做到「发过一次就不再发」。
        """
        rid = str(remote_id or "").strip()
        if not rid:
            return {}
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, status, reply FROM moment_comments
                         WHERE moment_tid=? AND COALESCE(remote_id,'')=? LIMIT 1""",
                      (moment_tid or "", rid))
            r = c.fetchone()
        if not r:
            return {}
        return {"id": int(r[0]), "status": r[1] or "", "reply": r[2] or ""}

    def comment_thread(self, root_id):
        """取一条线索的全部评论（按时间正序），用于拼上下文。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, commenter_name, content, reply, direction, created_at
                         FROM moment_comments
                         WHERE id=? OR COALESCE(root_id,0)=?
                         ORDER BY id ASC""", (int(root_id), int(root_id)))
            rows = c.fetchall()
        return [{"id": r[0], "name": r[1] or "", "content": r[2] or "",
                 "reply": r[3] or "", "direction": r[4] or "in",
                 "created_at": float(r[5] or 0)} for r in rows]

    def threads_awaiting_reply(self, limit=10):
        """找出"模型评论过、且对方最后一条还没回"的线索。

        判定：同一个 root_id 下，最后一条 direction='in'（对方发的）且 status='pending'。
        FeedBridge / comment_loop 用它实现"一直回到没有新回复"。
        """
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT COALESCE(root_id,0) AS rid, MAX(id) AS last_id
                         FROM moment_comments
                         GROUP BY CASE WHEN COALESCE(root_id,0)=0 THEN id ELSE root_id END
                         HAVING rid > 0
                         ORDER BY last_id DESC LIMIT ?""", (int(limit) * 4,))
            roots = [int(r[0]) for r in c.fetchall() if r[0]]
            out = []
            for rid in roots:
                c.execute("""SELECT id, direction, status FROM moment_comments
                             WHERE id=? OR COALESCE(root_id,0)=? ORDER BY id DESC LIMIT 1""",
                          (rid, rid))
                row = c.fetchone()
                if not row:
                    continue
                if (row[1] or "in") == "in" and (row[2] or "") == self.COMMENT_PENDING:
                    out.append({"root_id": rid, "last_id": int(row[0])})
                if len(out) >= limit:
                    break
        return out

    def delete_comment(self, comment_id):
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM moment_comments WHERE id=?", (int(comment_id),))
            self.conn.commit()
            return c.rowcount > 0

    def count_comments(self, status=None):
        with self.lock:
            c = self.conn.cursor()
            if status:
                c.execute("SELECT COUNT(*) FROM moment_comments WHERE status=?", (status,))
            else:
                c.execute("SELECT COUNT(*) FROM moment_comments")
            return c.fetchone()[0]

    # ==================== v7：浏览别人动态 ====================
    FEED_SEEN = "seen"
    FEED_LIKED = "liked"
    FEED_REPLIED = "replied"
    FEED_SKIPPED = "skipped"

    def feed_mark(self, post_key, source="", author_id="", author_name="",
                  content="", images="", action="", reply="", status=None):
        """记一条"看过的动态"。已存在则更新互动结果，返回 (id, is_new)。"""
        post_key = (post_key or "").strip()
        if not post_key:
            return 0, False
        now = time.time()
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT id FROM feed_seen WHERE post_key=?", (post_key,))
            row = c.fetchone()
            if row:
                sets, args = [], []
                for col, val in (("action", action), ("reply", reply), ("status", status)):
                    if val:
                        sets.append(f"{col}=?")
                        args.append(val)
                if sets:
                    sets.append("updated_at=?")
                    args.append(now)
                    args.append(int(row[0]))
                    c.execute(f"UPDATE feed_seen SET {', '.join(sets)} WHERE id=?", args)
                    self.conn.commit()
                return int(row[0]), False
            c.execute("""INSERT INTO feed_seen
                         (post_key, source, author_id, author_name, content, images,
                          action, reply, status, created_at, updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (post_key, source or "", str(author_id or ""), author_name or "",
                       (content or "")[:2000], images or "", action or "", reply or "",
                       status or self.FEED_SEEN, now, now))
            self.conn.commit()
            return int(c.lastrowid), True

    def feed_has(self, post_key):
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT 1 FROM feed_seen WHERE post_key=?", (post_key,))
            return c.fetchone() is not None

    def list_feed(self, limit=60, status=None):
        with self.lock:
            c = self.conn.cursor()
            if status:
                c.execute("""SELECT id, post_key, source, author_name, author_id, content,
                                    action, reply, status, created_at
                             FROM feed_seen WHERE status=? ORDER BY id DESC LIMIT ?""",
                          (status, int(limit)))
            else:
                c.execute("""SELECT id, post_key, source, author_name, author_id, content,
                                    action, reply, status, created_at
                             FROM feed_seen ORDER BY id DESC LIMIT ?""", (int(limit),))
            rows = c.fetchall()
        return [{"id": r[0], "post_key": r[1] or "", "source": r[2] or "",
                 "author_name": r[3] or "", "author_id": r[4] or "",
                 "content": r[5] or "", "action": r[6] or "", "reply": r[7] or "",
                 "status": r[8] or "seen", "created_at": float(r[9] or 0)}
                for r in rows]

    def count_feed(self, action=None):
        with self.lock:
            c = self.conn.cursor()
            if action:
                c.execute("SELECT COUNT(*) FROM feed_seen WHERE action=?", (action,))
            else:
                c.execute("SELECT COUNT(*) FROM feed_seen")
            return c.fetchone()[0]

    def delete_feed(self, feed_id):
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM feed_seen WHERE id=?", (int(feed_id),))
            self.conn.commit()
            return c.rowcount > 0

    # ==================== 统计 ====================
    def stats(self):
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT COUNT(*) FROM memories")
            m = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM chat_log")
            l = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM messages")
            msg = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM user_events WHERE status='pending'")
            ev = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM perm_memory")
            perm = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM diary")
            dia = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM care_plans WHERE status='pending'")
            plans = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM moment_comments WHERE status='pending'")
            cmts = c.fetchone()[0]
            try:
                c.execute("SELECT COUNT(*) FROM affinity")
                aff = c.fetchone()[0]
            except Exception:
                aff = 0
            return {"memories": m, "chat_log": l, "messages": msg,
                    "pending_events": ev, "perm_memory": perm, "diary": dia,
                    "pending_plans": plans, "pending_comments": cmts,
                    "affinity": aff}

    # ==================== v4：永久记忆 ====================
    PERM_CATEGORIES = ("identity", "preference", "relation", "plan", "health", "work", "other")
    PERM_STATUS_ACTIVE = "active"
    PERM_STATUS_SUPERSEDED = "superseded"

    @staticmethod
    def norm_fact(text):
        """把一句话压成"可比对的骨架"，用来判断两条记忆是不是在说同一件事。

        做什么：
          1. 去掉空白和标点
          2. 数字统一（全角→半角，2024年11月27日 → 20241127）
          3. 去掉"的/了/是/在/我/你/他/她"这类不承载信息的字

        为什么需要它：
          用户说"不对，我和她在一起的日子是24年11月27日"，
          旧记忆是"与名为梅梅的人是恋爱关系，纪念日 2024-11-27"，
          新提取的是"和梅梅在一起的纪念日其实是2023年11月27日"。
          两条字面不同但说的是同一件事 —— 靠骨架比对才能认出冲突。
        """
        s = str(text or "")
        if not s:
            return ""
        # 全角数字/字母转半角
        s = s.translate(str.maketrans(
            "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
            "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
            "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"))
        # 日期归一：2024年11月27日 / 2024-11-27 / 2024/11/27 / 11月27日
        s = re.sub(r"(\d{2,4})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*[日号]?",
                   r"\1\2\3", s)
        s = s.replace("年", "").replace("月", "").replace("日", "").replace("号", "")
        # 只留中文、字母、数字
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
        # 去停用字（这些字几乎不出现在"关键信息"里）
        for ch in "的了是在有和与及也都就还又很非常我你他她它这那么吗呢吧啊呀哦哪其":
            s = s.replace(ch, "")
        return s.lower()[:120]

    @staticmethod
    def _bigrams(s):
        """字符 2-gram 集合（比单字集合更能反映"说的是不是同一件事"）"""
        if not s:
            return set()
        if len(s) == 1:
            return {s}
        return {s[i:i + 2] for i in range(len(s) - 1)}

    @staticmethod
    def _digit_run(s):
        """抽出文本里的数字串（>=4 位，多半是日期/年份）"""
        return {m for m in re.findall(r"\d{4,}", s or "")}

    def _perm_conflict_ids(self, uid, category, norm_key, content):
        """找出可能与新事实冲突的旧记忆 id（同账号、同分类、说的是同一件事）。

        判据（任一成立即算冲突）：
          1. 骨架完全相同 → 一定冲突（同一件事换了个说法）
          2. 一个是另一个的子串，且短的不少于长的一半 → 大概率同一件事
          3. 2-gram Jaccard >= 0.50 → 措辞不同但主体一致
          4. 双方都带 4 位以上数字，且 2-gram Jaccard >= 0.22
             （处理"纪念日是2024年11月27日" vs "其实是2023年11月27日"：
               年份不同但说的都是那个纪念日，必须让新的顶掉旧的）
        """
        if not norm_key:
            return []
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, content, norm_key FROM perm_memory
                         WHERE user_id=? AND category=? AND status='active'""",
                      (str(uid), category))
            rows = c.fetchall()
        hits = []
        nk = set(self._bigrams(norm_key))
        ndig = self._digit_run(norm_key)
        for pid, old_content, old_key in rows:
            ok = str(old_key or self.norm_fact(old_content))
            if not ok:
                continue
            if ok == norm_key:
                hits.append(int(pid))
                continue
            short, long_ = (ok, norm_key) if len(ok) <= len(norm_key) else (norm_key, ok)
            if short and short in long_ and len(short) * 2 >= len(long_):
                hits.append(int(pid))
                continue
            ograms = set(self._bigrams(ok))
            inter = len(nk & ograms)
            union = len(nk | ograms) or 1
            jac = inter / union
            if jac >= 0.50:
                hits.append(int(pid))
                continue
            if ndig and self._digit_run(ok) and jac >= 0.22:
                hits.append(int(pid))
        return hits

    def add_perm(self, user_id, content, category="other", importance=3, source=None,
                 replace_conflict=False, supersede_ids=None):
        """写一条永久记忆。

        返回 (id, is_new, superseded_ids)。

        replace_conflict=True 时：若发现和旧记忆说同一件事（但内容不同），
        把旧条目标成 superseded，新条目生效 —— 这就是用户要的"改一下"。
          例：旧「纪念日是2024年11月27日」+ 新「和梅梅在一起的纪念日其实是2023年11月27日」
              → 旧的作废，新的生效，注入 prompt 时只剩新的。

        supersede_ids: 显式指定要顶掉的旧条目（模型在提炼时可以直接给 id，
        比启发式更准 —— 这是首选路径）。
        """
        content = (content or "").strip()
        if not content:
            return 0, False, []
        content = content[:500]
        uid = str(user_id)
        category = category if category in self.PERM_CATEGORIES else "other"
        try:
            importance = max(1, min(5, int(importance)))
        except (TypeError, ValueError):
            importance = 3
        norm_key = self.norm_fact(content)

        with self.lock:
            c = self.conn.cursor()
            # ① 完全同文 → 去重（只是刷新使用记录）
            c.execute("""SELECT id FROM perm_memory
                         WHERE user_id=? AND content=? AND status='active' LIMIT 1""",
                      (uid, content))
            row = c.fetchone()
            if row:
                c.execute("UPDATE perm_memory SET use_count=use_count+1, last_used=? "
                          "WHERE id=?", (time.time(), row[0]))
                self.conn.commit()
                return int(row[0]), False, []

            # ② 找冲突：显式指定的优先，否则按启发式
            superseded = []
            if supersede_ids:
                try:
                    want = [int(x) for x in supersede_ids if str(x).strip().isdigit()]
                except (TypeError, ValueError):
                    want = []
                if want:
                    qs = ",".join("?" * len(want))
                    c.execute(f"""SELECT id FROM perm_memory
                                  WHERE user_id=? AND status='active' AND id IN ({qs})""",
                              [uid] + want)
                    superseded = [int(r[0]) for r in c.fetchall()]
            if not superseded and replace_conflict:
                superseded = self._perm_conflict_ids(uid, category, norm_key, content)

            # ③ 插入新条目
            now = time.time()
            c.execute("""INSERT INTO perm_memory
                         (user_id, category, content, importance, source, created_at,
                          last_used, use_count, status, supersedes, norm_key, updated_at)
                         VALUES (?,?,?,?,?,?,?,0,'active',?,?,?)""",
                      (uid, category, content, importance, source, now, now,
                       superseded[0] if superseded else 0, norm_key, now))
            new_id = int(c.lastrowid or 0)

            # ④ 旧条目标作废（保留内容，方便用户在界面上看到"已被替换"）
            if superseded:
                c.executemany("UPDATE perm_memory SET status='superseded', updated_at=? "
                              "WHERE id=?", [(now, pid) for pid in superseded])
            self.conn.commit()
        return new_id, True, superseded

    def get_perm(self, user_id, limit=40, category=None, include_superseded=False):
        """取某账号的永久记忆，按重要度 + 新近排序。默认只返回有效的。"""
        uid = str(user_id)
        where = "user_id=?"
        args = [uid]
        if category:
            where += " AND category=?"
            args.append(category)
        if not include_superseded:
            where += " AND status='active'"
        args.append(int(limit))
        with self.lock:
            c = self.conn.cursor()
            c.execute(f"""SELECT id, category, content, importance, created_at, use_count,
                                 status, supersedes, updated_at
                          FROM perm_memory WHERE {where}
                          ORDER BY importance DESC, id DESC LIMIT ?""", args)
            rows = c.fetchall()
            if rows:
                c.executemany("UPDATE perm_memory SET last_used=?, use_count=use_count+1 "
                              "WHERE id=?", [(time.time(), r[0]) for r in rows])
                self.conn.commit()
        return [{"id": r[0], "category": r[1], "content": r[2],
                 "importance": r[3], "created_at": float(r[4] or 0),
                 "use_count": r[5], "status": r[6] or "active",
                 "supersedes": int(r[7] or 0),
                 "updated_at": float(r[8] or 0)} for r in rows]

    def update_perm(self, perm_id, content=None, category=None, importance=None):
        """手改一条永久记忆（界面上直接编辑）。返回 (ok, msg)。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT id, content, category, importance FROM perm_memory WHERE id=?",
                      (int(perm_id),))
            row = c.fetchone()
            if not row:
                return False, f"没有这条记忆：#{perm_id}"
            new_content = row[1] if content is None else str(content).strip()
            if not new_content:
                return False, "内容不能为空"
            new_cat = row[2] if category is None else (
                category if category in self.PERM_CATEGORIES else "other")
            try:
                new_imp = row[3] if importance is None else max(1, min(5, int(importance)))
            except (TypeError, ValueError):
                new_imp = row[3]
            c.execute("""UPDATE perm_memory SET content=?, category=?, importance=?,
                         norm_key=?, updated_at=? WHERE id=?""",
                      (new_content[:500], new_cat, new_imp,
                       self.norm_fact(new_content), time.time(), int(perm_id)))
            self.conn.commit()
        return True, "已保存"

    def count_perm(self, user_id=None, include_superseded=False):
        with self.lock:
            c = self.conn.cursor()
            sql = "SELECT COUNT(*) FROM perm_memory"
            cond, args = [], []
            if user_id:
                cond.append("user_id=?")
                args.append(str(user_id))
            if not include_superseded:
                cond.append("status='active'")
            if cond:
                sql += " WHERE " + " AND ".join(cond)
            c.execute(sql, args)
            return c.fetchone()[0]

    def delete_perm(self, perm_id):
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM perm_memory WHERE id=?", (int(perm_id),))
            self.conn.commit()
            return c.rowcount > 0

    def clear_perm(self, user_id):
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM perm_memory WHERE user_id=?", (str(user_id),))
            self.conn.commit()
            # v1.8.0：永久记忆清了，图谱里的点也一起清（否则会留下孤立的关系）
            try:
                import kg as _kgmod2
                _kgmod2.clear_user(user_id, mem=self)
            except Exception:
                pass
            return c.rowcount

    def perm_users(self):
        """有哪些账号有永久记忆：[{"user_id","n","last"}]"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT user_id, COUNT(*), MAX(created_at)
                         FROM perm_memory GROUP BY user_id ORDER BY 3 DESC""")
            return [{"user_id": r[0], "n": r[1], "last": float(r[2] or 0)}
                    for r in c.fetchall()]

    # ==================== v7：永久记忆 / 临时记忆互转 ====================
    def archive_perm(self, perm_id, archived=True):
        """把一条永久记忆停用/恢复（停用后不再注入 prompt，但内容保留）。

        返回 (ok, msg)。
        """
        status = "archived" if archived else "active"
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT content FROM perm_memory WHERE id=?", (int(perm_id),))
            row = c.fetchone()
            if not row:
                return False, f"没有这条记忆：#{perm_id}"
            c.execute("UPDATE perm_memory SET status=?, updated_at=? WHERE id=?",
                      (status, time.time(), int(perm_id)))
            self.conn.commit()
        return True, ("已转成临时记忆" if archived else "已恢复为永久记忆")

    def demote_to_temp(self, user_id, perm_ids, session_key=None):
        """永久记忆 → 临时记忆。

        做法：①永久条目 status='archived'（不再注入长期记忆）
              ②把内容作为一条 system 消息写进该会话的临时上下文，
                这样它还在"最近聊天"里，模型照样看得见，但会随滚动窗口被挤掉。
        返回 (ok, 条数, msg)。
        """
        uid = str(user_id or "")
        if not uid:
            return False, 0, "没有指定账号"
        ids = []
        for x in (perm_ids or []):
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not ids:
            return False, 0, "没有选中任何记忆"
        key = session_key or f"private_{uid}"
        n = 0
        with self.lock:
            c = self.conn.cursor()
            for pid in ids:
                c.execute("SELECT content FROM perm_memory WHERE id=? AND user_id=?",
                          (pid, uid))
                row = c.fetchone()
                if not row:
                    continue
                c.execute("UPDATE perm_memory SET status='archived', updated_at=? WHERE id=?",
                          (time.time(), pid))
                n += 1
            self.conn.commit()
        for pid in ids:
            try:
                c2 = self.conn.cursor()
                c2.execute("SELECT content FROM perm_memory WHERE id=?", (pid,))
                r = c2.fetchone()
                if r and r[0]:
                    self.add_message(key, "system", f"（以前记住的，现在只当临时上下文）{r[0]}")
            except Exception:
                pass
        return True, n, f"已把 {n} 条永久记忆转成临时记忆"

    def promote_texts(self, user_id, items, category="other", importance=3,
                      replace_conflict=True):
        """临时记忆 → 永久记忆。

        items 是 [{"content":..., "category":..., "importance":...}] 或纯字符串列表。
        返回 (ok, 新增条数, 替换条数, msg)。
        """
        uid = str(user_id or "")
        if not uid:
            return False, 0, 0, "没有指定账号"
        added = replaced = 0
        for it in (items or []):
            if isinstance(it, dict):
                content = str(it.get("content") or "").strip()
                cat = it.get("category") or category
                try:
                    imp = max(1, min(5, int(it.get("importance") or importance)))
                except (TypeError, ValueError):
                    imp = importance
            else:
                content = str(it or "").strip()
                cat, imp = category, importance
            if not content:
                continue
            try:
                _pid, is_new, sup = self.add_perm(
                    uid, content, category=cat, importance=imp, source="manual",
                    replace_conflict=replace_conflict)
            except Exception:
                continue
            if is_new:
                added += 1
                replaced += len(sup or [])
        if not added:
            return False, 0, 0, "没有新增（内容为空，或这几条早就记过了）"
        msg = f"已转成永久记忆 {added} 条"
        if replaced:
            msg += f"（顺带替换了 {replaced} 条旧记录）"
        return True, added, replaced, msg

    # ==================== v4：日记 ====================
    def add_diary(self, user_id, day, content, title="", mood="", msg_count=0,
                  slot=""):
        """写一篇日记/记忆快照。

        v1.9.10：同账号同一天可以有多篇（按 slot 区分）；slot 为空时保持老语义
        ——「同一天覆盖」，老调用方不用改。
        """
        content = (content or "").strip()
        if not content:
            return 0
        uid = str(user_id)
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM diary WHERE user_id=? AND day=? AND COALESCE(slot,'')=?",
                      (uid, str(day), str(slot or "")))
            c.execute("""INSERT INTO diary
                         (user_id, day, title, content, mood, msg_count, created_at, slot)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (uid, str(day), (title or "")[:120], content,
                       (mood or "")[:40], int(msg_count or 0), time.time(),
                       str(slot or "")))
            self.conn.commit()
            return int(c.lastrowid or 0)

    def list_diary(self, user_id=None, limit=60):
        with self.lock:
            c = self.conn.cursor()
            if user_id:
                c.execute("""SELECT id, user_id, day, title, content, mood, msg_count, created_at
                             FROM diary WHERE user_id=? ORDER BY day DESC, id DESC LIMIT ?""",
                          (str(user_id), int(limit)))
            else:
                c.execute("""SELECT id, user_id, day, title, content, mood, msg_count, created_at
                             FROM diary ORDER BY day DESC, id DESC LIMIT ?""", (int(limit),))
            rows = c.fetchall()
        return [{"id": r[0], "user_id": r[1], "day": r[2], "title": r[3],
                 "content": r[4], "mood": r[5], "msg_count": r[6],
                 "created_at": float(r[7] or 0)} for r in rows]

    def recent_diary(self, user_id="", limit=3, hours=0):
        """最近几篇日记/记忆快照（v1.9.10：给「记忆碎片」注入用）。

        hours>0 时只取这个时间窗内的。返回 [{id, day, slot, title, content, mood, ts}]
        """
        with self.lock:
            c = self.conn.cursor()
            sql = ("SELECT id, day, COALESCE(slot,''), COALESCE(title,''), content,"
                   " COALESCE(mood,''), COALESCE(created_at,0) FROM diary")
            args = []
            where = []
            if user_id:
                where.append("user_id=?")
                args.append(str(user_id))
            if hours:
                where.append("COALESCE(created_at,0) >= ?")
                args.append(time.time() - float(hours) * 3600)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY COALESCE(created_at,0) DESC, id DESC LIMIT ?"
            args.append(int(limit))
            c.execute(sql, tuple(args))
            out = []
            for r in c.fetchall():
                out.append({"id": r[0], "day": r[1], "slot": r[2], "title": r[3],
                            "content": r[4], "mood": r[5], "ts": float(r[6] or 0)})
            return out

    def get_diary(self, user_id, day):
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT id, user_id, day, title, content, mood, msg_count, created_at
                         FROM diary WHERE user_id=? AND day=? LIMIT 1""",
                      (str(user_id), str(day)))
            r = c.fetchone()
        if not r:
            return None
        return {"id": r[0], "user_id": r[1], "day": r[2], "title": r[3],
                "content": r[4], "mood": r[5], "msg_count": r[6],
                "created_at": float(r[7] or 0)}

    def delete_diary(self, diary_id):
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM diary WHERE id=?", (int(diary_id),))
            self.conn.commit()
            return c.rowcount > 0

    def count_diary(self, user_id=None):
        with self.lock:
            c = self.conn.cursor()
            if user_id:
                c.execute("SELECT COUNT(*) FROM diary WHERE user_id=?", (str(user_id),))
            else:
                c.execute("SELECT COUNT(*) FROM diary")
            return c.fetchone()[0]

    # ==================== v4：临时上下文查询（供 UI 展示） ====================
    def recent_messages_all(self, limit=80):
        """全库最近消息（跨会话），供"临时读取记忆列表"用。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT session_key, role, content, ts FROM messages
                         ORDER BY id DESC LIMIT ?""", (int(limit),))
            rows = c.fetchall()
        return [{"session_key": r[0], "role": r[1], "content": r[2],
                 "ts": float(r[3] or 0)} for r in rows]

    def messages_of(self, session_key, limit=40):
        """某个会话最近的消息（正序），供 UI 展开看。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT role, content, ts FROM messages
                         WHERE session_key=? ORDER BY id DESC LIMIT ?""",
                      (session_key, int(limit)))
            rows = c.fetchall()
        return [{"role": r[0], "content": r[1], "ts": float(r[2] or 0)}
                for r in reversed(rows)]

    def session_stats(self):
        """每个会话的消息条数与最后时间，供 UI 分组展示临时记忆。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT session_key, COUNT(*), MAX(ts) FROM messages
                         GROUP BY session_key ORDER BY 3 DESC""")
            return [{"session_key": r[0], "n": r[1], "last": float(r[2] or 0)}
                    for r in c.fetchall()]

    def get_perm_list(self, limit=200, include_superseded=False):
        """全库永久记忆（跨账号），供「永久记忆列表」用。"""
        sql = """SELECT id, user_id, category, content, importance,
                        source, created_at, use_count, status, updated_at
                 FROM perm_memory"""
        if not include_superseded:
            sql += " WHERE status='active'"
        sql += " ORDER BY importance DESC, id DESC LIMIT ?"
        with self.lock:
            c = self.conn.cursor()
            c.execute(sql, (int(limit),))
            return [{"id": r[0], "user_id": r[1], "category": r[2], "content": r[3],
                     "importance": r[4], "source": r[5],
                     "created_at": float(r[6] or 0), "use_count": r[7],
                     "status": r[8] or "active",
                     "updated_at": float(r[9] or 0)}
                    for r in c.fetchall()]

    # ==================== v8：好感度 ====================
    # 分数 → 关系等级。改这里就能调整"多久算熟人"。
    AFFINITY_LEVELS = (
        (0, "stranger", "陌生人"),
        (10, "acquaintance", "认识的人"),
        (30, "familiar", "熟人"),
        (60, "friend", "朋友"),
        (120, "close_friend", "好朋友"),
        (200, "confidant", "知己"),
        (350, "special", "特别的人"),
    )

    @classmethod
    def affinity_levels(cls):
        """v1.3.0 ⑳ 关系等级表可自定义。

        配置项 ``affinity_levels`` 写成 ``[[分数, 名字], ...]``（也吃 dict 形态），
        填了就用用户的，没填/填坏了就退回内置这 7 档。
        key 仍按内置顺序取（stranger/familiar/...），这样 agent 里那套
        语气映射不会因为改名而失效；超出 7 档的部分用 lv7、lv8…
        """
        try:
            import config as _cfg_mod
            raw = _cfg_mod.get("affinity_levels") or []
            rows = []
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                for i, it in enumerate(raw):
                    th = cn = None
                    if isinstance(it, (list, tuple)) and len(it) >= 2:
                        th, cn = it[0], it[1]
                    elif isinstance(it, dict):
                        th = it.get("min", it.get("score"))
                        cn = it.get("cn") or it.get("name")
                    try:
                        th = int(float(th))
                    except Exception:
                        continue
                    cn = str(cn or "").strip()
                    if not cn:
                        continue
                    key = (cls.AFFINITY_LEVELS[i][1]
                           if i < len(cls.AFFINITY_LEVELS) else "lv%d" % i)
                    rows.append((th, key, cn))
            if len(rows) >= 2:
                rows.sort(key=lambda x: x[0])
                return tuple(rows)
        except Exception:
            pass
        return cls.AFFINITY_LEVELS

    @classmethod
    def affinity_level(cls, score):
        """(level_key, 中文名) —— 分数越高越亲近（v1.3.0：等级表可自定义）"""
        s = int(score or 0)
        levels = cls.affinity_levels()
        key, cn = levels[0][1], levels[0][2]
        for th, k, c in levels:
            if s >= th:
                key, cn = k, c
        return key, cn

    def get_affinity(self, user_id, name=""):
        """读一个人的好感度；没有就返回 0 分的默认值（不写库）。"""
        uid = str(user_id or "").strip()
        if not uid:
            return {"user_id": "", "name": name, "score": 0, "level": "stranger",
                    "level_cn": "陌生人", "msgs": 0, "positive": 0, "negative": 0,
                    "created_at": 0.0, "updated_at": 0.0}
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT user_id, name, score, msgs, positive, negative,
                                created_at, updated_at
                         FROM affinity WHERE user_id=?""", (uid,))
            r = c.fetchone()
        if not r:
            return {"user_id": uid, "name": name, "score": 0, "level": "stranger",
                    "level_cn": "陌生人", "msgs": 0, "positive": 0, "negative": 0,
                    "created_at": 0.0, "updated_at": 0.0}
        key, cn = self.affinity_level(r[2])
        return {"user_id": r[0], "name": r[1] or name, "score": int(r[2] or 0),
                "level": key, "level_cn": cn, "msgs": int(r[3] or 0),
                "positive": int(r[4] or 0), "negative": int(r[5] or 0),
                "created_at": float(r[6] or 0), "updated_at": float(r[7] or 0)}

    def bump_affinity(self, user_id, name="", delta=1, positive=False, negative=False,
                      cap=1000):
        """加减好感度，返回加完之后的记录。

        delta 可以是正负；positive/negative 只是打个标记方便统计"说过多少好话"。
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {}
        d = int(delta or 0)
        now = time.time()
        with self.lock:
            c = self.conn.cursor()
            # V1.0.0-Beta · BUG-07：新建记录就把「今天」写上。
            # 以前这里 last_decay_day 是 NULL，而 decay 用 COALESCE(...,'') 比较，
            # 于是新用户当天就会被扣一次分 —— 用户体感是「刚聊完天分就掉了」。
            c.execute("""INSERT OR IGNORE INTO affinity(user_id, name, score, level, msgs,
                                              positive, negative, created_at, updated_at,
                                              last_decay_day)
                         VALUES(?,?,0,'stranger',0,0,0,?,?,?)""",
                      (uid, str(name or ""), now, now, time.strftime("%Y-%m-%d")))
            c.execute("""UPDATE affinity
                         SET score = MAX(0, MIN(?, score + ?)),
                             name = CASE WHEN ?='' THEN name ELSE ? END,
                             msgs = msgs + 1,
                             positive = positive + ?,
                             negative = negative + ?,
                             updated_at = ?,
                             last_decay_day = ?
                         WHERE user_id=?""",
                      (int(cap or 1000), d, str(name or ""), str(name or ""),
                       1 if positive else 0, 1 if negative else 0, now,
                       time.strftime("%Y-%m-%d"), uid))
            c.execute("SELECT score FROM affinity WHERE user_id=?", (uid,))
            row = c.fetchone()
            score = int(row[0] or 0) if row else 0
            key, cn = self.affinity_level(score)
            c.execute("UPDATE affinity SET level=? WHERE user_id=?", (key, uid))
            self.conn.commit()
        out = self.get_affinity(uid, name)
        out["level_cn"] = cn
        return out

    def set_affinity(self, user_id, score, name=""):
        """直接设定分数（界面手改 / 导入用）"""
        uid = str(user_id or "").strip()
        if not uid:
            return False
        try:
            s = max(0, int(score))
        except (TypeError, ValueError):
            return False
        key, _cn = self.affinity_level(s)
        now = time.time()
        with self.lock:
            c = self.conn.cursor()
            c.execute("""INSERT INTO affinity(user_id, name, score, level, created_at, updated_at,
                                          last_decay_day)
                         VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET
                            score=excluded.score, level=excluded.level,
                            name=CASE WHEN excluded.name='' THEN affinity.name ELSE excluded.name END,
                            updated_at=excluded.updated_at,
                            last_decay_day=excluded.last_decay_day""",
                      (uid, str(name or ""), s, key, now, now,
                       time.strftime("%Y-%m-%d")))
            self.conn.commit()
        return True

    def decay_affinity(self, per_day=1):
        """每天掉一点（久不联系就生疏）。同一自然日只掉一次。"""
        d = int(per_day or 0)
        if d <= 0:
            return 0
        today = time.strftime("%Y-%m-%d")
        changed = 0
        with self.lock:
            c = self.conn.cursor()
            # BUG-07：只减「有 last_decay_day 且不是今天」的人。
            # 用 COALESCE(last_decay_day,'') <> today 的话，NULL 也会被算进去，
            # 于是从没衰减过的老用户第一次跑就白掉一天的分。
            c.execute("""SELECT user_id, score FROM affinity
                         WHERE last_decay_day IS NOT NULL AND last_decay_day <> ?""",
                      (today,))
            for uid, score in c.fetchall():
                ns = max(0, int(score or 0) - d)
                key, _cn = self.affinity_level(ns)
                c.execute("""UPDATE affinity SET score=?, level=?, last_decay_day=?
                             WHERE user_id=?""", (ns, key, today, uid))
                changed += 1
            self.conn.commit()
        return changed

    def list_affinity(self, limit=100):
        """好感度看板用：按分数倒序"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT user_id, name, score, msgs, positive, negative,
                                created_at, updated_at
                         FROM affinity ORDER BY score DESC, updated_at DESC LIMIT ?""",
                      (int(limit),))
            out = []
            for r in c.fetchall():
                key, cn = self.affinity_level(r[2])
                out.append({"user_id": r[0], "name": r[1] or "", "score": int(r[2] or 0),
                            "level": key, "level_cn": cn, "msgs": int(r[3] or 0),
                            "positive": int(r[4] or 0), "negative": int(r[5] or 0),
                            "created_at": float(r[6] or 0), "updated_at": float(r[7] or 0)})
            return out

            return out

    # ==================== v1.1.4：伴侣关系（和好感度分表，互不覆盖） ====================
    # 好感度管「分数/语气」，伴侣关系管「身份/称呼/边界」。分数还是分数，
    # 关系是关系——用户把好感度清零也不会把「恋人」身份一起删掉。
    ROMANCE_LEVELS = (("friend", "朋友"), ("close", "挚友"),
                      ("like", "心动"), ("lover", "恋人"), ("partner", "伴侣"))

    def romance_level_cn(self, level):
        for k, cn in self.ROMANCE_LEVELS:
            if k == level:
                return cn
        return "朋友"

    def get_romance(self, user_id):
        """读某个人的伴侣关系；没有记录时返回默认 friend（不写库）。"""
        uid = str(user_id or "").strip()
        base = {"user_id": uid, "level": "friend", "level_cn": "朋友",
                "since": 0.0, "note": "", "updated_at": 0.0, "auto": 1}
        if not uid:
            return base
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT user_id, level, since, note, updated_at, auto
                         FROM romance_state WHERE user_id=?""", (uid,))
            r = c.fetchone()
        if not r:
            return base
        lv = str(r[1] or "friend")
        return {"user_id": r[0], "level": lv, "level_cn": self.romance_level_cn(lv),
                "since": float(r[2] or 0), "note": r[3] or "",
                "updated_at": float(r[4] or 0),
                "auto": 1 if (r[5] is None or int(r[5]) != 0) else 0}

    def set_romance(self, user_id, level=None, note=None, auto=0):
        """设定伴侣关系。level 为空只改备注；返回 (ok, msg, 记录)。

        v1.1.6：auto=1 表示「让分数自动决定」（用户在界面上勾了自动），
        之后聊天里的好感度变化会自动升级/降级；auto=0 是用户亲手钉死的，
        自动逻辑再也不会覆盖它。"""
        uid = str(user_id or "").strip()
        if not uid:
            return False, "没有用户", {}
        cur = self.get_romance(uid)
        lv = str(level if level is not None else cur["level"]).strip()
        valid = [k for k, _cn in self.ROMANCE_LEVELS]
        if lv not in valid:
            return False, f"关系只能是 {'/'.join(valid)}", cur
        nt = str(note if note is not None else cur["note"] or "")[:200]
        now = time.time()
        since = float(cur.get("since") or 0) or now
        ao = 1 if auto else 0
        with self.lock:
            c = self.conn.cursor()
            c.execute("""INSERT INTO romance_state(user_id, level, since, note, updated_at, auto)
                         VALUES(?,?,?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET
                           level=excluded.level, note=excluded.note,
                           auto=excluded.auto, updated_at=excluded.updated_at""",
                      (uid, lv, since, nt, now, ao))
            self.conn.commit()
        return True, f"关系已设为「{self.romance_level_cn(lv)}」", self.get_romance(uid)

    @staticmethod
    def romance_level_for_score(score):
        """分数 → 关系档。门槛故意拉得比好感度等级宽，避免每天跳来跳去。"""
        try:
            sc = float(score or 0)
        except Exception:
            sc = 0.0
        if sc >= 1200:
            return "partner"
        if sc >= 600:
            return "lover"
        if sc >= 300:
            return "like"
        if sc >= 120:
            return "close"
        return "friend"

    def sync_romance(self, user_id, score):
        """按分数自动更新关系（只对 auto=1 的记录生效）。

        返回 (changed, level, msg)。用户手动钉过的（auto=0）原样不动。"""
        uid = str(user_id or "").strip()
        if not uid:
            return False, "friend", ""
        cur = self.get_romance(uid)
        if int(cur.get("auto", 1)) != 1:
            return False, cur.get("level") or "friend", "手动设定，不自动改"
        want = self.romance_level_for_score(score)
        if want == (cur.get("level") or "friend"):
            return False, want, ""
        ok, msg, rec = self.set_romance(uid, level=want,
                                        note=cur.get("note") or "", auto=1)
        if not ok:
            return False, cur.get("level") or "friend", msg
        old_cn = self.romance_level_cn(cur.get("level") or "friend")
        new_cn = self.romance_level_cn(want)
        return True, want, f"关系从「{old_cn}」变成「{new_cn}」"

    def delete_romance(self, user_id):
        """删掉一条伴侣关系（回到默认「朋友」，不影响好感度分数）。"""
        uid = str(user_id or "").strip()
        if not uid:
            return False, "没有用户"
        with self.lock:
            c = self.conn.cursor()
            c.execute("DELETE FROM romance_state WHERE user_id=?", (uid,))
            self.conn.commit()
        return True, "关系已清除（回到「朋友」）"

    def list_romance(self, limit=100):
        """列出所有非「朋友」的关系，给好感度看板用。"""
        with self.lock:
            c = self.conn.cursor()
            c.execute("""SELECT r.user_id, COALESCE(a.name,''), r.level, r.since,
                                r.note, r.updated_at, r.auto
                         FROM romance_state r LEFT JOIN affinity a ON a.user_id=r.user_id
                         WHERE r.level <> 'friend'
                         ORDER BY r.updated_at DESC LIMIT ?""", (int(limit),))
            rows = c.fetchall()
        return [{"user_id": r[0], "name": r[1] or "", "level": r[2],
                 "level_cn": self.romance_level_cn(r[2]),
                 "since": float(r[3] or 0), "note": r[4] or "",
                 "updated_at": float(r[5] or 0),
                 "auto": 1 if (r[6] is None or int(r[6]) != 0) else 0} for r in rows]

    def reset_affinity(self, user_id=None):
        """清空一个人的好感度；user_id 为空就清空全部"""
        with self.lock:
            c = self.conn.cursor()
            if user_id:
                c.execute("DELETE FROM affinity WHERE user_id=?", (str(user_id),))
            else:
                c.execute("DELETE FROM affinity")
            n = c.rowcount
            self.conn.commit()
        return n

    def affinity_summary(self, user_id, name="", show_score=True):
        """给 system prompt 用的一句话（没好感度就返回空串）"""
        a = self.get_affinity(user_id, name)
        if not a.get("user_id") or not a.get("msgs"):
            return ""
        who = a.get("name") or name or "对方"
        if show_score:
            return "%s：好感度 %d（%s），已经聊过 %d 轮" % (
                who, a["score"], a["level_cn"], a["msgs"])
        return "%s：关系是「%s」，已经聊过 %d 轮" % (who, a["level_cn"], a["msgs"])

    # ==================== v5：记忆导出 / 导入（备份与迁移） ====================
    def export_memory(self, user_id=None, include_diary=True, include_events=False):
        """把记忆导成一个可 JSON 序列化的字典（备份 / 换电脑 / 迁移用）。

        默认含：永久记忆 + 日记；可选：事件。
        临时会话消息（messages 表）不导 —— 那是缓存，没必要备份。
        """
        out = {
            "format": "qq-agent-memory",
            "version": 2,
            "exported_at": time.time(),
            "exported_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": str(user_id) if user_id else "",
            "perm": [],
            "diary": [],
            "events": [],
        }
        with self.lock:
            c = self.conn.cursor()
            if user_id:
                c.execute("""SELECT user_id, category, content, importance, source,
                                    created_at, status, updated_at
                             FROM perm_memory WHERE user_id=? ORDER BY id""",
                          (str(user_id),))
            else:
                c.execute("""SELECT user_id, category, content, importance, source,
                                    created_at, status, updated_at
                             FROM perm_memory ORDER BY id""")
            out["perm"] = [{
                "user_id": r[0], "category": r[1], "content": r[2],
                "importance": r[3], "source": r[4],
                "created_at": float(r[5] or 0),
                "status": r[6] or "active",
                "updated_at": float(r[7] or 0),
            } for r in c.fetchall()]

            if include_diary:
                if user_id:
                    c.execute("""SELECT user_id, day, title, content, mood, msg_count, created_at
                                 FROM diary WHERE user_id=? ORDER BY day""", (str(user_id),))
                else:
                    c.execute("""SELECT user_id, day, title, content, mood, msg_count, created_at
                                 FROM diary ORDER BY day""")
                out["diary"] = [{
                    "user_id": r[0], "day": r[1], "title": r[2] or "",
                    "content": r[3], "mood": r[4] or "",
                    "msg_count": r[5] or 0, "created_at": float(r[6] or 0),
                } for r in c.fetchall()]

            if include_events:
                if user_id:
                    c.execute("""SELECT user_id, event_desc, target_time, status, created_at
                                 FROM user_events WHERE user_id=? ORDER BY target_time""",
                              (str(user_id),))
                else:
                    c.execute("""SELECT user_id, event_desc, target_time, status, created_at
                                 FROM user_events ORDER BY target_time""")
                out["events"] = [{
                    "user_id": r[0], "event_desc": r[1],
                    "target_time": float(r[2] or 0), "status": r[3],
                    "created_at": float(r[4] or 0),
                } for r in c.fetchall()]
        return out

    def import_memory(self, data, user_id=None, mode="merge", replace_conflict=True):
        """导入 export_memory 产出的字典。

        mode:
          merge   = 只补缺（已存在的同文记忆跳过），推荐
          replace = 先清空该账号的永久记忆，再整批写入（"载入成永久记忆"）

        user_id: 强制把所有条目归到这个账号（换号迁移用）；None 则按文件里的。
        返回 {"ok","perm_added","perm_superseded","diary_added","msg"}
        """
        if not isinstance(data, dict) or data.get("format") != "qq-agent-memory":
            return {"ok": False, "perm_added": 0, "diary_added": 0,
                    "msg": "不是本程序导出的记忆文件（缺少 format 标记）"}
        if mode not in ("merge", "replace"):
            return {"ok": False, "perm_added": 0, "diary_added": 0,
                    "msg": f"mode 只能是 merge / replace，收到 {mode!r}"}

        target_uid = str(user_id) if user_id else None
        perm_added = 0
        sup_total = 0
        diary_added = 0

        if mode == "replace":
            uids = {target_uid} if target_uid else {
                str(it.get("user_id")) for it in (data.get("perm") or []) if it.get("user_id")}
            for u in uids:
                if u:
                    self.clear_perm(u)

        for it in (data.get("perm") or []):
            if not isinstance(it, dict):
                continue
            uid = target_uid or str(it.get("user_id") or "").strip()
            content = str(it.get("content") or "").strip()
            if not uid or not content:
                continue
            pid, is_new, sup = self.add_perm(
                uid, content,
                category=str(it.get("category") or "other"),
                importance=it.get("importance") or 3,
                source=str(it.get("source") or "import"),
                replace_conflict=bool(replace_conflict))
            if is_new:
                perm_added += 1
                sup_total += len(sup)
            # 导入的历史条目如果原本是 superseded，也标成 superseded
            if str(it.get("status") or "active") == "superseded" and pid:
                with self.lock:
                    c = self.conn.cursor()
                    c.execute("UPDATE perm_memory SET status='superseded' WHERE id=?", (pid,))
                    self.conn.commit()

        for it in (data.get("diary") or []):
            if not isinstance(it, dict):
                continue
            uid = target_uid or str(it.get("user_id") or "").strip()
            day = str(it.get("day") or "").strip()
            content = str(it.get("content") or "").strip()
            if not uid or not day or not content:
                continue
            self.add_diary(uid, day, content,
                           title=str(it.get("title") or ""),
                           mood=str(it.get("mood") or ""),
                           msg_count=int(it.get("msg_count") or 0))
            diary_added += 1

        return {
            "ok": True,
            "perm_added": perm_added,
            "perm_superseded": sup_total,
            "diary_added": diary_added,
            "msg": (f"导入完成：永久记忆 +{perm_added}"
                    + (f"（替换旧条目 {sup_total} 条）" if sup_total else "")
                    + (f"，日记 +{diary_added}" if diary_added else "")),
        }

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    m = Memory()
    m.add_fact("test", "小明", "在准备N2考试")
    m.add_fact("test", "小明", "在准备N2考试")   # 重复，应跳过
    m.add_fact("test", "小明", "喜欢蓝发角色")
    print("facts:", m.get_facts("test"))
    eid = m.add_event("123456", "明天9点考试", time.time() + 3600, "private_123456")
    print("event id:", eid, "due:", m.get_due_events())
    print("events:", m.list_events())
    print("stats:", m.stats())
    print("clear:", m.clear_scope("test"))
    print("stats:", m.stats())
