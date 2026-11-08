import os
import sys
import atexit
import subprocess
import threading
import time

import pystray
from PIL import Image, ImageDraw


def get_base_path():
    """兼容 PyInstaller 打包后 __file__ 变化
    开发环境：返回脚本所在目录
    打包后：返回 sys.executable 所在目录
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_path()
AGENT_SCRIPT = os.path.join(BASE_DIR, "agent.py")
LOG_FILE = os.path.join(BASE_DIR, "agent.log")
START_BAT = os.path.join(BASE_DIR, "start.bat")

agent_proc = None
icon = None
_shutdown = False


def _ensure_utf8():
    """确保 stderr 输出 UTF-8（Windows 偶尔会回退到 cp850）"""
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _read_last_lines(path, n=5):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except Exception:
        return []


def start_agent():
    global agent_proc
    if agent_proc and agent_proc.poll() is None:
        return  # 已在运行
    # 打开日志文件用于追加，避免阻塞
    log_fh = open(LOG_FILE, "a", encoding="utf-8")
    agent_proc = subprocess.Popen(
        [sys.executable, AGENT_SCRIPT],
        cwd=BASE_DIR,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    atexit.register(stop_agent)
    # 轻量等待：确保进程已经真正启动（最多 8 秒）
    for _ in range(80):
        if agent_proc.poll() is not None:
            break
        time.sleep(0.1)


def stop_agent():
    global agent_proc, _shutdown
    _shutdown = True
    if agent_proc and agent_proc.poll() is None:
        agent_proc.terminate()
        try:
            agent_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent_proc.kill()
        agent_proc = None
    else:
        agent_proc = None


def restart_agent(icon=None, item=None):
    stop_agent()
    start_agent()


def open_log(icon=None, item=None):
    if os.path.exists(LOG_FILE):
        os.startfile(LOG_FILE)


def quit_app(icon=None, item=None):
    stop_agent()
    if icon:
        icon.stop()


def create_image():
    """用 Pillow 生成一个纯色占位图标（也可替换为真实的鲸鱼 png）"""
    image = Image.new("RGB", (64, 64), color=(0, 120, 215))
    draw = ImageDraw.Draw(image)
    draw.ellipse((16, 16, 48, 48), fill=(255, 255, 255))
    return image


def setup_tray():
    global icon
    image = create_image()
    menu = pystray.Menu(
        pystray.MenuItem("查看日志", open_log),
        pystray.MenuItem("重启 Agent", restart_agent),
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("小鲸鱼", image, "小鲸鱼 Agent", menu)


if __name__ == "__main__":
    _ensure_utf8()
    start_agent()
    setup_tray()
    # atexit 已在 start_agent 中注册；此处 icon.run 会阻塞，确保子进程在退出时被清理
    icon.run()
    # icon.run 返回后（用户点退出），确保清理
    stop_agent()