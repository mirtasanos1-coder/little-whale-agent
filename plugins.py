# -*- coding: utf-8 -*-
"""插件宿主（v1.1.2）。

两件事：
  1. 把程序自己就带着的能力登记成「内置插件」，让用户在设置里看得见、能关掉；
  2. 从「数据目录/plugins/*.py」加载用户插件，加载失败只记错误、不炸主程序。

用户插件写法（详见 插件开发指南.md）：

    PLUGIN = {"name": "我的插件", "version": "1.0", "desc": "干什么的", "author": "你"}

    def setup(api):      # 可选：载入时跑一次
        api.log("载入成功")

    def run(api):        # 可选：界面上点「运行」时跑
        api.notify("你好", "插件跑起来了")

安全边界（写清楚，别让用户误以为插件是沙箱）：
  插件是**本机 Python 代码**，跟你手动 double-click 一个 .py 一样有全部权限。
  所以只放自己写的 / 完全信任的插件进去。
"""
import importlib.util
import os
import sys
import traceback

import config as cfg_mod

_LOADED = {}     # 文件名 -> {"ok":bool,"error":str,"module":module}
_SETUP_DONE = set()
_ERR = {}


def plugin_dir():
    """用户插件目录：数据目录/plugins/"""
    d = os.path.join(cfg_mod.DATA_DIR, "plugins")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


BUILTIN = (
    {"key": "chat", "name": "聊天回复", "desc": "收到消息 → 拼上下文 → 调模型 → 回话（核心，关了就不能聊天）", "version": "1.1.2", "core": True},
    {"key": "memory", "name": "记忆提取", "desc": "把对话里值得记的事写进永久记忆", "version": "1.1.2"},
    {"key": "event", "name": "待办提取", "desc": "从对话里认出「几点要做什么」并记成待办", "version": "1.1.2"},
    {"key": "diary", "name": "写日记", "desc": "按天把对话总结成日记", "version": "1.1.2"},
    {"key": "learn", "name": "学习提炼", "desc": "从近期对话里提炼可复用的经验", "version": "1.1.2"},
    {"key": "qzone", "name": "动态互动", "desc": "读动态、点赞、评论、回评论", "version": "1.1.2"},
    {"key": "imagegen", "name": "画图", "desc": "接云端绘图 API，按需要画一张发出去", "version": "1.1.2"},
    {"key": "tray", "name": "托盘常驻", "desc": "关窗口缩到托盘，点图标恢复", "version": "1.1.2"},
    {"key": "backup", "name": "备份与导入", "desc": "记忆/日记的定时导出、丢文件夹自动导入", "version": "1.1.2"},
    {"key": "usage", "name": "用量统计", "desc": "记账、看板、导出 CSV", "version": "1.1.2"},
)


def enabled_map():
    m = cfg_mod.load().get("plugin_enabled")
    return m if isinstance(m, dict) else {}


def is_enabled(name_or_key, default=True):
    m = enabled_map()
    key = str(name_or_key or "")
    if key in m:
        return bool(m[key])
    return bool(default)


def set_enabled(name, on):
    """开关一个插件。核心插件（聊天回复）不允许关。"""
    key = str(name or "").strip()
    if not key:
        return False, "插件名不能空"
    for b in BUILTIN:
        if key in (b["key"], b["name"]) and b.get("core"):
            return False, f"{b['name']} 是核心功能，关掉就不能聊了"
    m = dict(enabled_map())
    m[key] = bool(on)
    ok, msg, _v = cfg_mod.set_checked("plugin_enabled", m)
    return (True, f"「{key}」已{'启用' if on else '停用'}") if ok else (False, msg)


class PluginApi:
    """塞给插件的小工具，避免插件去 import agent 内部。"""

    def __init__(self, name=""):
        self.name = name

    def log(self, msg, level="INFO"):
        line = f"[插件:{self.name}] {msg}"
        try:
            with open(os.path.join(cfg_mod.DATA_DIR, "plugin.log"), "a", encoding="utf-8") as f:
                import datetime
                f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except Exception:
            print(line)

    def cfg_get(self, key, default=None):
        return cfg_mod.load().get(key, default)

    def cfg_set(self, key, value):
        return cfg_mod.set_checked(key, value)

    def data_dir(self):
        return cfg_mod.DATA_DIR

    def notify(self, title, body=""):
        self.log(f"{title} {body}".strip())

    def ask_model(self, prompt, max_tokens=400):
        """让模型说一句（走全局模型/功能分工）。插件里最常用的就是这个。"""
        import agent
        return agent.ask_model_raw(prompt, max_tokens=max_tokens)


def _load_one(path):
    """加载一个用户插件，异常都收住。"""
    name = os.path.basename(path)
    if name in _LOADED:
        return _LOADED[name]
    info = {"ok": False, "error": "", "module": None, "file": name}
    try:
        mod_name = "qq_agent_plugin_" + os.path.splitext(name)[0]
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("这个文件不像 Python 模块")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        info["module"] = mod
        info["ok"] = True
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        _ERR[name] = traceback.format_exc()
    _LOADED[name] = info
    return info


def _plugin_meta(mod, name):
    meta = getattr(mod, "PLUGIN", None)
    if not isinstance(meta, dict):
        meta = {}
    return {
        "name": str(meta.get("name") or os.path.splitext(name)[0]),
        "version": str(meta.get("version") or "0"),
        "desc": str(meta.get("desc") or "（这个插件没写 desc）"),
        "author": str(meta.get("author") or ""),
        "has_run": bool(callable(getattr(mod, "run", None))),
        "has_setup": bool(callable(getattr(mod, "setup", None))),
    }


def list_plugins():
    """内置 + 用户插件，附带状态与错误。"""
    out = []
    for b in BUILTIN:
        item = dict(b)
        item["enabled"] = is_enabled(b["key"], True)
        item["builtin"] = True
        item["error"] = ""
        out.append(item)
    d = plugin_dir()
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.endswith(".py") and not f.startswith("_"))
    except Exception:
        files = []
    for f in files:
        info = _load_one(os.path.join(d, f))
        item = {"key": f, "file": f, "builtin": False,
                "enabled": is_enabled(f, True), "error": info["error"]}
        if info["ok"]:
            item.update(_plugin_meta(info["module"], f))
            item["key"] = item["name"]
            item["enabled"] = is_enabled(item["name"], is_enabled(f, True))
        else:
            item.update({"name": os.path.splitext(f)[0], "version": "?",
                         "desc": "（加载失败）", "author": "", "has_run": False, "has_setup": False})
        out.append(item)
    return out


def run_plugin(name):
    """跑用户插件的 run(api)。"""
    for item in list_plugins():
        if item.get("builtin"):
            continue
        if name not in (item.get("key"), item.get("name"), item.get("file")):
            continue
        info = _LOADED.get(item["file"]) or {}
        mod = info.get("module")
        if not mod:
            return False, f"「{name}」没加载成功：{info.get('error') or '未知原因'}"
        fn = getattr(mod, "run", None)
        if not callable(fn):
            return False, f"「{name}」没有 run() 函数，没什么可跑的"
        try:
            fn(PluginApi(item["name"]))
            return True, f"「{item['name']}」跑完了（输出在 plugin.log）"
        except Exception as e:
            _ERR[item["file"]] = traceback.format_exc()
            return False, f"「{item['name']}」跑出错：{type(e).__name__}: {e}"
    return False, f"没找到插件「{name}」"


def setup_all():
    """启动时把启用中的用户插件的 setup() 跑一遍。"""
    done = []
    for item in list_plugins():
        if item.get("builtin") or not item.get("enabled") or not item.get("has_setup"):
            continue
        info = _LOADED.get(item.get("file")) or {}
        mod = info.get("module")
        fn = getattr(mod, "setup", None) if mod else None
        if not callable(fn):
            continue
        if item["file"] in _SETUP_DONE:
            continue
        try:
            fn(PluginApi(item["name"]))
            _SETUP_DONE.add(item["file"])
            done.append(item["name"])
        except Exception as e:
            _ERR[item["file"]] = traceback.format_exc()
            print(f"[插件] {item['name']} setup 失败: {e}")
    return done


def reload_plugins():
    """重新扫目录（新放的插件不用重启）。"""
    _LOADED.clear()
    _ERR.clear()
    _SETUP_DONE.clear()
    return list_plugins()


def errors():
    return dict(_ERR)


if __name__ == "__main__":
    import json
    for x in list_plugins():
        print(json.dumps({k: x.get(k) for k in ("name", "version", "enabled", "builtin", "error")},
                         ensure_ascii=False))
