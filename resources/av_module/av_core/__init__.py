"""模拟音视频通话模块 —— 独立微服务（端口 5800）。

与主控台完全解耦：不 import agent.py / backend_server.py / Tauri 侧任何代码，
只通过 HTTP(JSON) 通信。
"""

__version__ = "1.0.0"
