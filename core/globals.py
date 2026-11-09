# -*- coding: utf-8 -*-
"""活配置 / 活运行时对象的桥 —— 拆分后唯一允许「回头看 agent」的地方。

为什么必须有这个模块（拆 agent.py 时最先撞上的坑）：

  agent.py 里有两类东西**不能**跟着函数一起搬走：

  ① 配置派生常量（LM_MODEL / LM_MAX_TOKENS / NAPCAT_HTTP …）
     它们不是 import 时定死的：`agent._reload_constants()` 会在配置文件指纹变化后
     就地刷新它们（这是 V1.0.0-Beta 修 BUG-01「54 个配置项只写盘不生效」的办法）。
     如果 core 里的模块写 `from agent import LM_MODEL`，拿到的是**导入那一瞬间的快照**，
     用户在设置页改完、重启前就再也变不了 —— 等于把那个老 BUG 又请回来了。

  ② 运行时对象（napcat 连接、mem 数据库、sessions 会话表、state 状态字典 …）
     进程启动后才赋值，重连 / 重载之后还会被换成新对象。

所以 core 各模块统一用本模块的 `live()` / `call()` **在用的那一刻**去取 agent 的同名属性：
拿到的一定是当前值，不会再出现「快照」问题，也不需要 agent 反过来 import core 里的东西
（避免循环引用）。

注意：`live()` 每次都是一次 sys.modules 查表 + getattr，微秒级。
网络/IO 热点（发消息、调模型）里随便用；如果哪天要在每秒几十万次的纯计算循环里用，
再在那边缓存一次即可。
"""


def _agent():
    """拿到 agent 模块本体（延迟导入，避免 import 期循环）。"""
    import sys

    mod = sys.modules.get("agent")
    if mod is not None:
        return mod
    try:
        import agent as mod  # noqa: F401  （首次导入时把模块塞进 sys.modules）
        return mod
    except Exception:
        return None


def live(name, default=None):
    """取 agent.py 里这个全局名字的**当前值**。"""
    mod = _agent()
    if mod is None:
        return default
    return getattr(mod, name, default)


def has(name):
    """agent.py 里现在有没有这个名字。"""
    mod = _agent()
    return mod is not None and hasattr(mod, name)


def call(name, *args, **kwargs):
    """调用 agent.py 里的一个函数（延迟取，永远是最新那份）。"""
    fn = live(name)
    if fn is None:
        raise AttributeError("agent 里没有 %s 这个函数" % name)
    return fn(*args, **kwargs)


def cfg(key, default=None):
    """读一个「实时配置」（等价于 agent._live_cfg(key, default)）。

    配置文件改了以后最多 3 秒就生效 —— 和拆分前完全一致。
    """
    fn = live("_live_cfg")
    if fn is None:
        return default
    try:
        return fn(key, default)
    except Exception:
        return default


def sync(names, target_globals, defaults=None):
    """把 agent 里这些「活名字」的最新值灌进**本模块**的 globals。

    为什么需要它：core 里的函数体是从 agent.py 原样搬过来的，里面写的是
    `LM_MODEL`、`mem`、`SYSTEM_PROMPT` 这种**裸名字**。裸名字在函数里走的是
    本模块的 `__dict__`（不是 `__getattr__`，PEP 562 管不到函数体内的全局查找），
    所以要么把函数体一行行改成 `_g.live(...)`（改动量大、容易抄错），
    要么在每次调用前把最新值同步过来（本函数）—— 这里选后者：函数体一个字不改。

    用法（每个模块一次，然后在入口函数开头调 `_sync()`）：

        _SYNC_NAMES = ("LM_MODEL", "LM_BASE", "mem")
        _sync = lambda: _g.sync(_SYNC_NAMES, globals())

    代价：一次 dict 查找 + getattr，约几微秒；这些入口都是网络/IO 级别，可忽略。
    """
    mod = _agent()
    if mod is None:
        return 0
    d = defaults or {}
    n = 0
    for name in names:
        if hasattr(mod, name):
            target_globals[name] = getattr(mod, name)
            n += 1
        elif name in d:
            target_globals[name] = d[name]
    return n


def set_live(name, value):
    """反过来：把 core 里算出来的值写回 agent 的同名全局。

    目前只有一个用处 —— `refresh_system_prompt()` 搬进 core.prompt_builder 之后，
    它生成的 SYSTEM_PROMPT 必须回填到 agent，否则 agent 里几十处读 SYSTEM_PROMPT
    的地方会读到空串（拆分前这个函数就在 agent 里，写的是 agent 的全局）。
    """
    mod = _agent()
    if mod is None:
        return False
    setattr(mod, name, value)
    return True

