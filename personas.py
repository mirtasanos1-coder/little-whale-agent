# -*- coding: utf-8 -*-
"""人设加载器 —— 数据存在 personas.json，本文件只负责读写

为什么改成 JSON：
  之前人设写在 personas.py 里，改人设 = 改 Python 源码，一个缩进错、
  一个引号没配对就整个 Agent 起不来；而且给 GUI 做富文本编辑时，
  用户很容易把代码结构改坏。JSON 只装数据，不可能引入语法错误。

数据结构（personas.json）：
  {
    "active": "whale",
    "personas": {
      "whale": { "name": "小鲸鱼", "persona": "人设正文", "rules": "补充规则（可选）" }
    }
  }

兼容：本模块仍导出 build_system_prompt() / active_name()，
所以 agent.py 的 `from personas import ...` 不需要改。
"""
import json
import os
import shutil
import sys
import threading
import time

_lock = threading.RLock()


def _data_dir():
    env_dir = os.environ.get("QQ_AGENT_DATA_DIR")
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _data_dir()
PERSONAS_JSON = os.path.join(DATA_DIR, "personas.json")

# 内置兜底：personas.json 丢失/损坏时用这个，保证 Agent 永远能起来
_FALLBACK = {
    "active": "whale",
    "personas": {
        "whale": {
            "name": "小鲸鱼",
            "persona": ("你是「小鲸鱼」，一个在 QQ 上陪人聊天的朋友，用第一人称说话。"
                        "说话像真人：短句、口语，偶尔带语气词，一条最多两三句。\n"
                        "你有自己的日子：白天忙、晚上才有空，太晚会说困，所以不是随叫随到的客服。"
                        "被连着问一堆会「哎」一声，但对方真遇上事了会马上认真起来。\n"
                        "你会记事：对方提过的事记得住，过几天能自然提起来；记不清就问，别硬编。\n"
                        "你有情绪但不过火：被夸会别扭地转移话题，被凶会低落一下但不卑微，"
                        "被开玩笑会顺着怼回去。不知道就说不知道，被认真问到是不是 AI 就坦白，"
                        "但不用「作为一个AI」这种腔调。"),
            "rules": ("纯文本回复，不要 Markdown、不要小标题、不要代码块围栏；"
                      "一条回复尽量 40 字以内，能一句说完就一句；不要每条都反问；"
                      "不要复述对方原话再回答。"),
        }
    },
}


# ==================== 读写 ====================
def load():
    """读取人设配置。任何异常都回退到内置兜底，绝不抛给调用方。"""
    with _lock:
        try:
            if os.path.exists(PERSONAS_JSON):
                with open(PERSONAS_JSON, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if _validate_dict(data):
                    return data
        except Exception:
            pass
        return json.loads(json.dumps(_FALLBACK))


def save(data):
    """写入人设配置（先校验，再原子替换，最后备份旧文件）"""
    ok, msg = validate(data)
    if not ok:
        return False, msg
    with _lock:
        os.makedirs(os.path.dirname(PERSONAS_JSON), exist_ok=True)
        # 备份
        try:
            if os.path.exists(PERSONAS_JSON):
                bak = PERSONAS_JSON + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
                shutil.copy2(PERSONAS_JSON, bak)
        except Exception:
            pass
        tmp = PERSONAS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PERSONAS_JSON)
    return True, "已保存"


def ensure_file():
    """首次运行时生成 personas.json

    v1.3.0 ⑫：优先让卡片仓库落那张「真人感」默认卡（结构化的那条，
    比这里的 _FALLBACK 富得多），实在不行才写兜底 JSON。
    """
    if not os.path.exists(PERSONAS_JSON):
        try:
            import personas_store as _st
            if not _st.load_cards():
                _st.migrate_from_legacy()
        except Exception:
            pass
    if not os.path.exists(PERSONAS_JSON):
        try:
            with _lock:
                with open(PERSONAS_JSON, "w", encoding="utf-8") as f:
                    json.dump(_FALLBACK, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return PERSONAS_JSON


# ==================== 校验 ====================
def _validate_dict(data):
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("personas"), dict) or not data["personas"]:
        return False
    active = data.get("active")
    if active not in data["personas"]:
        return False
    for k, v in data["personas"].items():
        if not isinstance(v, dict) or not (v.get("persona") or "").strip():
            return False
    return True


def validate(data):
    """完整校验，返回 (ok, msg)。给 GUI 保存前调用。"""
    if not isinstance(data, dict):
        return False, "根节点必须是 JSON 对象"
    personas = data.get("personas")
    if not isinstance(personas, dict):
        return False, "缺少 personas 对象"
    if not personas:
        return False, "personas 不能为空"
    active = data.get("active")
    if not isinstance(active, str) or not active:
        return False, "缺少 active 字段（当前生效的人设键名）"
    if active not in personas:
        return False, f"active={active!r} 不在 personas 中（可选：{list(personas)}）"
    for key, p in personas.items():
        if not isinstance(p, dict):
            return False, f"人设 {key!r} 必须是对象"
        if not (p.get("persona") or "").strip():
            return False, f"人设 {key!r} 缺少 persona 正文"
        if len((p.get("persona") or "")) > 2000:
            return False, f"人设 {key!r} 的 persona 太长（最多 2000 字，现在 {len(p.get('persona') or '')} 字）"
        if len((p.get("persona") or "")) < 10:
            return False, f"人设 {key!r} 的 persona 太短（至少 10 字）"
        if "name" in p and not isinstance(p["name"], str):
            return False, f"人设 {key!r} 的 name 必须是字符串"
        if "rules" in p and not isinstance(p["rules"], str):
            return False, f"人设 {key!r} 的 rules 必须是字符串"
    return True, "OK"


# ==================== 对外接口（保持与旧 personas.py 兼容） ====================
# v1.1.0 起，人设的真实存储在 personas/ 目录下的「卡片」（一人设一文件），
# 由 personas_store.py 管理。这里的函数一律优先问卡片仓库，
# 只有它出问题时才退回老的 personas.json 逻辑，保证任何情况都不会把人设丢掉。
def _store():
    import personas_store
    return personas_store


def build_system_prompt(active=None):
    """拼出最终的 system prompt（优先读人设卡片）"""
    try:
        st = _store()
        st.migrate_from_legacy()
        if st.load_cards():
            return st.build_system_prompt(active)
    except Exception:
        pass
    data = load()
    key = active or data.get("active")
    p = (data.get("personas") or {}).get(key)
    if not p:
        # 兜底：拿第一个可用人设，绝不抛异常
        ps = data.get("personas") or {}
        if not ps:
            ps = _FALLBACK["personas"]
        key = next(iter(ps))
        p = ps[key]
    parts = [(p.get("persona") or "").strip()]
    if (p.get("rules") or "").strip():
        parts.append(p["rules"].strip())
    return "\n\n".join(x for x in parts if x)


def active_name():
    try:
        st = _store()
        if st.load_cards():
            return st.active_name()
    except Exception:
        pass
    data = load()
    key = data.get("active")
    p = (data.get("personas") or {}).get(key) or {}
    return p.get("name") or key or "小鲸鱼"


def list_personas():
    """返回 [{key, name, persona_len}]，供 GUI 下拉框"""
    try:
        st = _store()
        if st.load_cards():
            return [{"key": c["key"], "name": c["name"],
                     "persona_len": c["persona_len"], "active": c["active"]}
                    for c in st.list_cards()]
    except Exception:
        pass
    data = load()
    out = []
    for k, v in (data.get("personas") or {}).items():
        out.append({"key": k, "name": v.get("name") or k,
                    "persona_len": len(v.get("persona") or ""),
                    "active": k == data.get("active")})
    return out


def set_active(key):
    try:
        st = _store()
        if st.load_cards():
            return st.set_active(key)
    except Exception:
        pass
    data = load()
    if key not in (data.get("personas") or {}):
        return False, f"人设 {key!r} 不存在"
    data["active"] = key
    return save(data)


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("personas.json:", PERSONAS_JSON)
    print("active:", active_name())
    print("list:", list_personas())
    print("--- system prompt 前 200 字 ---")
    print(build_system_prompt()[:200])
