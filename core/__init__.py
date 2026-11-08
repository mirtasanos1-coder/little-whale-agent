# -*- coding: utf-8 -*-
"""小鲸鱼 Agent 的模块化拆分包（V1.9.26 起）。

拆分原则（非常关键，后续任何人都要遵守）：
  1. **只做物理搬运，不改业务逻辑**。函数体一行都不重写，连行号都记在注释里。
  2. **agent.py 顶部保留兼容层**：拆出去的名字会在 agent.py 里重新 import 一遍，
     所以外部（app_ui.py / napcat_mgr.py / 插件…）继续写 `agent.ask_model()` 不会坏。
  3. **不能用 `from agent import XXX` 拿配置派生常量**。那些常量是
     `agent._reload_constants()` 在设置变更后就地刷新的，快照会「改完不生效」。
     core 里的模块要现取，用 `core.globals.live("XXX")`。

现役模块：
  core.globals  —— 活配置 / 活运行时对象的桥（唯一允许跨模块取 agent 状态的地方）
  core.utils    —— 纯函数与工具：日志、文本清洗、时间换算、消息规范化、卡片解析、连发合并
  core.sender   —— 消息投递：私聊/群聊/语音/图片/表情包 + 拟人发送层
"""
