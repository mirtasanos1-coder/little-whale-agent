# -*- coding: utf-8 -*-
"""开机自启动管理 —— 不需要管理员权限

为什么用注册表 HKCU\\...\\Run 而不是计划任务：
  旧版用的是「计划任务 + autostart.bat」，那玩意儿必须管理员权限才能装/卸，
  而且删的时候报"拒绝访问"，很烦。HKCU 下的 Run 键属于当前用户，
  读写都不需要提权，删掉也是一句话。

行为：
  · 打包版（sys.frozen）→ 自启动项指向 exe 本身，参数 --autostart
    （--autostart 表示"启动后最小化到托盘/直接开 Agent"，具体见 app_ui.main）
  · 源码版 → 指向 pythonw.exe + app_ui.py，避免弹黑窗口
  · 每次 enable 都会先删旧的再写新的，不会堆积重复项

对外 API：
  is_supported()            -> bool          （只有 Windows 支持）
  is_enabled()              -> bool
  current_command()         -> str           注册表里现在写的命令行
  enable()                  -> (ok, msg)
  disable()                 -> (ok, msg)
  apply(enabled)            -> (ok, msg)     幂等：已经是目标状态就不动
  status()                  -> dict          给界面用
"""
import os
import subprocess
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE_NAME = "小鲸鱼Agent"


def is_supported():
    return os.name == "nt"


# ==================== 注册表读写 ====================
def _read_all():
    """读 HKCU Run 下的全部值，返回 {name: command}。读不到就返回 {}"""
    if not is_supported():
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    name, value, _typ = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[name] = value
                i += 1
    except OSError:
        pass
    return out


def _write_value(name, command):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, command)


def _delete_value(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ==================== 命令行构造 ====================
def _pythonw():
    """优先用 pythonw.exe（不弹控制台）"""
    exe = sys.executable or ""
    if not exe:
        return "pythonw.exe"
    d, base = os.path.split(exe)
    if base.lower() == "python.exe":
        cand = os.path.join(d, "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return exe


def _launcher_path():
    """v1.2.x：真正该被开机拉起来的程序。

    Tauri 版里 Python 只是后端（backend_server.exe），开机自启必须拉起**桌面壳**，
    否则开机后只有个没有界面的后端在跑。桌面壳在 spawn 后端时会用环境变量
    QQ_AGENT_LAUNCHER 把自己的路径告诉后端，这里优先用它。
    """
    raw = (os.environ.get("QQ_AGENT_LAUNCHER") or "").strip().strip('"')
    if raw and os.path.isfile(raw):
        return raw
    return ""


def current_command():
    """当前应该写进注册表的命令行；返回空串表示「这台机器上没法自启」"""
    launcher = _launcher_path()
    if launcher:
        # 桌面版：拉起桌面壳本身，加 --autostart 让界面知道「我是被开机拉起来的」
        return f'"{launcher}" --autostart'
    if getattr(sys, "frozen", False):
        # 打包的后端被单独运行（没经过桌面壳）：不能把 backend_server.exe 写成自启项，
        # 否则开机只会起一个没界面的后端 —— 宁可明确告诉用户
        return ""
    # 源码模式：pythonw 拉起无头后端 + 浏览器界面（开发/调试用）
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_ui.py")
    return f'"{_pythonw()}" "{app}" --web'


def _normalize(cmd):
    return (cmd or "").strip().strip('"').lower()


# ==================== 对外 API ====================
def is_enabled():
    """注册表里有没有我们这一项（不看 config.json）"""
    if not is_supported():
        return False
    return APP_VALUE_NAME in _read_all()


def _actual_command():
    return _read_all().get(APP_VALUE_NAME, "")


def enable():
    if not is_supported():
        return False, "当前系统不支持（仅 Windows）"
    cmd = current_command()
    if not cmd:
        return False, ("找不到桌面程序（小鲸鱼.exe），没法设置开机自启。"
                       "请从桌面快捷方式启动小鲸鱼后再打开这个开关。")
    try:
        _write_value(APP_VALUE_NAME, cmd)
    except Exception as e:
        return False, f"写注册表失败：{e}"
    # 回读校验，确保真写进去了
    got = _actual_command()
    if _normalize(got) != _normalize(cmd):
        return False, f"写入后校验失败（读到 {got!r}）"
    return True, f"已开启开机自启动：{cmd}"


def disable():
    if not is_supported():
        return False, "当前系统不支持（仅 Windows）"
    existed = _delete_value(APP_VALUE_NAME)
    if is_enabled():
        return False, "删除失败，注册表项仍然存在（可能被安全软件保护）"
    return True, "已关闭开机自启动" if existed else "本来就没开启"


def apply(enabled):
    """幂等设置：想开就开，想关就关，已经是对的就什么都不做"""
    want = bool(enabled)
    now = is_enabled()
    if want and not current_command():
        # 关的时候不用管能不能拿到桌面程序路径；开就必须有
        return False, ("找不到桌面程序（小鲸鱼.exe），没法设置开机自启。"
                       "请从桌面快捷方式启动小鲸鱼后再打开这个开关。")
    if want == now:
        # 状态一致，但如果命令行变了（比如换了安装路径）顺手修一下
        if want and _normalize(_actual_command()) != _normalize(current_command()):
            try:
                _write_value(APP_VALUE_NAME, current_command())
                return True, "自启动路径已更新为当前程序位置"
            except Exception as e:
                return False, f"更新自启动路径失败：{e}"
        return True, "开机自启动已开启" if want else "开机自启动已关闭"
    return enable() if want else disable()


def status():
    """给界面用的完整状态"""
    try:
        import config as _c
        cfg_want = bool(_c.get("autostart", False))
    except Exception:
        cfg_want = False

    if not is_supported():
        return {"ok": False, "supported": False, "enabled": False,
                "config": cfg_want, "command": "", "name": APP_VALUE_NAME,
                "msg": "当前系统不支持（仅 Windows）"}

    enabled = is_enabled()
    actual = _actual_command()
    want = current_command()
    drift = enabled and _normalize(actual) != _normalize(want)
    msg = "已开启" if enabled else "未开启"
    if drift:
        msg = "已开启，但路径指向旧位置（点「修复路径」可更新）"
    if not want:
        msg = ("已开启，但指向的不是桌面程序" if enabled else
               "打包版：请从桌面快捷方式启动小鲸鱼后再设置（现在拿不到桌面程序路径）")
    return {
        "ok": True,
        "supported": True,
        "can_enable": bool(want),
        "enabled": enabled,
        "config": cfg_want,
        "command": actual,
        "expect": want,
        "drift": drift,
        "name": APP_VALUE_NAME,
        "key": f"HKCU\\{RUN_KEY}",
        "msg": msg,
    }


def repair():
    """把自启动路径修正为当前程序位置"""
    if not is_supported():
        return False, "当前系统不支持"
    if not is_enabled():
        return False, "当前没有开启自启动"
    try:
        _write_value(APP_VALUE_NAME, current_command())
        return True, "自启动路径已更新"
    except Exception as e:
        return False, f"更新失败：{e}"


def launch_on_boot_arg():
    """本进程是否由开机自启动拉起（--autostart）"""
    return "--autostart" in sys.argv[1:]


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("支持:", is_supported())
    print("状态:", status())
    print("应写:", current_command())
