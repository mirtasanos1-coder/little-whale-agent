# -*- coding: utf-8 -*-
"""
把 Python 后端（backend_server.py）打成 backend_server.exe。

为什么要打包：Tauri 桌面端只负责「开窗口 + 进程管理」，真正干活的是这个后端。
用户机器上不一定装了 Python，所以发布版必须是一个自带解释器的 exe。

用法：
    python build_backend.py              # 打包，并把产物复制到 desktop/src-tauri/resources/
    python build_backend.py --no-copy    # 只打包，不复制
    python build_backend.py --clean      # 先清掉 build/ dist/ 再打包

产物：
    dist/backend_server.exe                        （单文件，约 60~90MB，取决于导入的模块）
    desktop/src-tauri/resources/backend_server.exe （供 Tauri 打包时塞进安装目录）
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
EXE_NAME = "backend_server"
RES_DIR = os.path.join(HERE, "desktop", "src-tauri", "resources")

# uvicorn / fastapi 是「运行时按字符串导入」的重灾区，PyInstaller 静态分析看不出来，
# 漏掉任何一个的表现都是：能启动，但一收请求就 500 或者直接崩。
HIDDEN = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets",
    "websockets.legacy",
    "wsproto",
    "h11",
    "httptools",
    "fastapi",
    "starlette",
    "pydantic",
    "anyio",
    "app_ui",
    "agent",
    "memory",
    "config",
    "usage",
    "napcat_mgr",
    "web_server",
    "requests",
    "sqlite3",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
]

# agent.py / app_ui.py 里有一堆「用到才 import」的模块（函数内 import），
# PyInstaller 静态分析看不见它们，打包后会在运行时 ModuleNotFoundError。
# 表现：打包版「启动 Agent」直接崩（agent_crash.log 里写着 No module named 'requests'），
# 而源码模式一切正常 —— 这种坑最难查，所以这里一次性列全。
# 任务一（V1.0.0-Beta）：本地向量模型是懒 import（semantic._local_bge 里才 import），
# PyInstaller 静态分析看不见，必须显式声明，否则包里带了模型也加载不起来。
LAZY = [
    # v1.9.10：视频解析用 yt-dlp（函数内 import，PyInstaller 静态分析看不到）
    "yt_dlp",
    "yt_dlp.extractor",
    "yt_dlp.utils",
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "tokenizers",
    "numpy",

    "psutil",            # 进程管理
    "PIL",               # 图片处理（Pillow）
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "websocket",         # OneBot 正向 WS 客户端（websocket-client）
    "diary",
    "learn",
    "notice",
    "semantic",
    "imagegen",
    "qzone",
    "qzone_interact",
    "autostart_mgr",
    "personas",
    "personas_store",
    "plugins",
    "providers",
    "emotion",
    "emotion_engine",
    "subconscious",
    "stickers",
    "qq_faces",
    "formats",
    "i18n",
    "theme",
    "security",
    "secure_core",
    "updater",
    "uninstall",
    "mem_rebuild",
    "mem_transfer",
    "admin",
    "version",          # 版本号唯一来源（config/backend_server 都会 import）
    "av_bridge",        # V1.3.2：音视频微服务桥接（app_ui 里是函数内 import，静态分析看不到）
    "spark", "spark_crypto", "cryptography",
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.backends.openssl",            # V1.4.0：时光胶囊（app_ui 里是函数内 import）
    "computer_use",     # V1.4.0：Agent 工具集（agent.py 里是函数内 import）
    "qq_call",          # V1.4.0：主动打电话（同上）
    "route_memo",       # V1.4.0：高频话记录模式（agent.py 里是函数内 import）
    "agent_gate",       # V1.5.0：Agent 总闸门（app_ui/agent 函数内 import）
    "qq_approve",       # V1.5.0：聊天内授权
    "token_est",        # V1.5.1：消耗预估（app_ui 里函数内 import）
    "audit",           # V1.7.0: 签署审计（app_ui 里函数内 import）
    "kg",              # V1.8.0: 知识图谱（memory/diary/agent 里全是函数内 import）
    "networkx",        # V1.8.0: kg.load_graph 里函数内 import（没装也能跑，装了就必须打进去）
    "networkx.classes",
    "networkx.classes.multidigraph",
    # V1.8.12：真唱 / 自我操作 / 情感画像。这三个模块在 agent.py 和 app_ui.py 里
    # 全是函数内 import（`import sing` / `import selfmod` / `import emoprofile`），
    # PyInstaller 静态分析完全看不到 —— 不补进来，打包版点歌会崩在 ModuleNotFoundError。
    "sing",
    "selfmod",
    "emoprofile",

    # V1.9.17：NapCat WebUI 客户端。app_ui._webui_mod() 里是函数内 import，
    # 静态分析看不到 —— 不补进来，打包版点「扫码登录」会崩在 ModuleNotFoundError。
    "napcat_webui",

    # V1.5.2（QA No-Go 修复三）：Agent 联网搜索用的 Playwright。
    # computer_use.tool_web_search 是函数内 import，PyInstaller 静态分析完全看不到；
    # 少打一个 greenlet 的 C 扩展，打包版就会在「让 Agent 查天气」时崩在
    # `ModuleNotFoundError: No module named 'greenlet._greenlet'`。
    "playwright",
    "playwright.sync_api",
    "playwright._impl",
    "playwright._impl._api_types",
    "playwright._impl._sync_base",
    "playwright._impl._browser",
    "playwright._impl._playwright",
    "greenlet",
    "greenlet._greenlet",
    "pyee",
    "pyee.sync",
]

HIDDEN = HIDDEN + LAZY

# 明确排除的大件，能省 20MB 以上
EXCLUDE = [
    # 注意：numpy 不能排除 —— 本地向量模型（onnxruntime）就靠它（V1.0.0-Beta 任务一）
    "matplotlib", "pandas", "scipy", "PIL.ImageQt",
    "PyQt5", "PySide2", "PySide6", "notebook", "IPython", "pytest", "setuptools._distutils",
]


def run(cmd, **kw):
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=HERE, **kw)


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return True
    except Exception:
        pass
    print("[1/3] 没装 PyInstaller，先装一个 …")
    if run([sys.executable, "-m", "pip", "install", "pyinstaller", "--disable-pip-version-check"]) != 0:
        print("！！PyInstaller 安装失败，检查网络或 pip 源")
        return False
    return True


def ensure_edge_tts():
    """把语音引擎打进包里。

    打包版跑在 PyInstaller 自带解释器里，向导的「pip install」装到系统 python
    里它 import 不到（表现为「pip 说装完了却仍然未安装」）。所以在构建机上装好、
    再 hidden-import 进去，是最省事的解法。装不上也不算失败 —— 只是没语音。
    """
    try:
        import importlib.util
        if importlib.util.find_spec("edge_tts") is not None:
            print("[1/3] edge-tts 已就位，会一起打进包里")
            return True
    except Exception:
        pass
    print("[1/3] 没装 edge-tts，先装上（能一起打进包里，用户就不用自己 pip 了）…")
    if run([sys.executable, "-m", "pip", "install", "edge-tts", "--disable-pip-version-check"]) != 0:
        print("！！edge-tts 装不上，打出来的包就没语音（文字聊天不受影响）")
        return False
    return True


def _have_module(name: str) -> bool:
    """构建机上有没有这个模块（用来决定要不要 hidden-import）。"""
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _dir_has_files(path: str) -> bool:
    """目录里有没有真文件（递归看一眼，用于判断模型目录是不是空的）。"""
    try:
        for _root, _dirs, files in os.walk(path):
            if files:
                return True
    except Exception:
        pass
    return False


def _dir_size_mb(path: str) -> str:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return "%.1f MB" % (total / 1048576.0)


CY_TARGETS = ["secure_core", "spark_crypto", "security", "audit",
              "legacy_keys"]


def step_build_secret():
    """v1.9.2：生成构建期 SECRET，**不落盘**。

    以前这里会写一个 _build_secret.py，被 PyInstaller 打成普通 .pyc ——
    marshal 一读就拿到 32 字节原始密钥（报告实测）。现在改成环境变量传进去，
    由 build_cython.py 拼进 secure_core 的编译输入，直接编进 .pyd。
    """
    import base64
    import secrets
    tok = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    os.environ["XJY_BUILD_SECRET"] = tok
    print("    构建期 SECRET 已生成（编进 .pyd，不落盘）")
    return tok


def clean_build_secret():
    """构建完把 _build_secret.py 从源码树删掉 —— 它只该活在产物里。"""
    p = os.path.join(HERE, "_build_secret.py")
    try:
        if os.path.isfile(p):
            os.remove(p)
            print("    已清理 _build_secret.py（只在产物里保留）")
    except Exception as e:
        print("    ！清理 _build_secret.py 失败：%s" % e)


def step_cython():
    """v1.9.0：把安全相关的四个模块编成 .pyd，返回可用产物列表。

    编译失败不阻断打包 —— 只是这一版不带编译产物（继续用 .py）。
    """
    out = os.path.join(HERE, "_cy_build")
    script = os.path.join(HERE, "build_cython.py")
    if not os.path.isfile(script):
        print("    （没有 build_cython.py，跳过 Cython）")
        return []
    try:
        r = subprocess.run([sys.executable, script], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    except Exception as e:
        print("    Cython 调用失败：%s（继续用 .py）" % e)
        return []
    tail = (r.stdout or "")[-1200:]
    for line in tail.splitlines():
        if line.strip():
            print("    " + line.strip())
    if r.returncode != 0:
        print("    Cython 编译没成功（返回 %s），这一版继续用 .py" % r.returncode)
        if r.stderr:
            print("    " + r.stderr.strip().splitlines()[-1][:200])
        return []
    pyds = []
    for m in CY_TARGETS:
        p = os.path.join(out, m + ".pyd")
        if os.path.isfile(p):
            pyds.append(p)
    print("    已生成 %d 个 .pyd：%s" % (len(pyds), ", ".join(
        os.path.basename(x) for x in pyds)))
    return pyds


def write_version_file():
    """给 exe 打上「这是小鲸鱼」的版本信息。

    不加的话，第一次启动那一下 Windows 防火墙会弹窗问「是否允许 Python 访问网络」——
    小白看到 Python 两个字就不敢点。带上版本信息后，弹窗里显示的是
    「小鲸鱼 Agent 后端服务」，发布者也换成我们自己，点「允许」就踏实了。
    """
    if os.name != "nt":
        return ""
    # V1.3.2：文件版本也跟着 version.py 走，别再手写（以前写死 (1,2,1,0)）
    try:
        import version as _vm
        ver = tuple(_vm.VERSION_INFO)
    except Exception:
        ver = (1, 3, 2, 0)
    path = os.path.join(HERE, "build_version_info.txt")
    body = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(v)s,
    prodvers=%(v)s,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', '小鲸鱼'),
         StringStruct('FileDescription', '小鲸鱼 Agent 后端服务'),
         StringStruct('FileVersion', '%(dotted)s'),
         StringStruct('InternalName', 'backend_server'),
         StringStruct('LegalCopyright', '小鲸鱼'),
         StringStruct('OriginalFilename', 'backend_server.exe'),
         StringStruct('ProductName', '小鲸鱼 QQ Agent'),
         StringStruct('ProductVersion', '%(dotted)s')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % {"v": str(ver), "dotted": ".".join(str(x) for x in ver)}
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print("[1/3] 已生成版本信息（防火墙弹窗里会显示「小鲸鱼 Agent 后端服务」）")
        return path
    except Exception as e:
        print("！！版本信息写不出来（不影响打包）：%s" % e)
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-copy", action="store_true", help="不复制到 desktop/src-tauri/resources")
    ap.add_argument("--clean", action="store_true", help="先清理 build/ 与 dist/")
    ap.add_argument("--onefile", action="store_true", default=True, help="单文件模式（默认）")
    ap.add_argument("--onedir", action="store_true", help="目录模式，启动更快")
    a = ap.parse_args()

    if not os.path.isfile(os.path.join(HERE, "backend_server.py")):
        print("！！当前目录下找不到 backend_server.py，请在程序根目录运行本脚本")
        return 2

    if a.clean:
        for d in (BUILD, DIST):
            if os.path.isdir(d):
                print("清理 " + d)
                shutil.rmtree(d, ignore_errors=True)

    if not ensure_pyinstaller():
        return 3
    has_tts = ensure_edge_tts()
    ver_file = write_version_file()

    print("[2/3] 开始打包 …")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", EXE_NAME,
           "--onedir" if a.onedir else "--onefile",
           "--console",                       # Rust 端要读 stdout 里的端口行
           "--collect-submodules", "uvicorn",
           "--hidden-import", "sqlite3"]
    if ver_file:
        cmd += ["--version-file", ver_file]
    # 图标：任务管理器里再看到「Python」就太掉价了（用户反馈过两次）
    _icon = os.path.join(HERE, "desktop", "src-tauri", "icons", "icon.ico")
    if os.path.exists(_icon):
        cmd += ["--icon", _icon]
        print("    图标：%s" % _icon)
    hidden = list(HIDDEN)
    if has_tts:
        hidden += ["edge_tts", "edge_tts.communicate", "edge_tts.drm", "aiohttp"]
    # v1.9.25：唱歌要解 mp3（真唱会拆字变调，必须先拿到 PCM）。
    # miniaudio 只解不编、连 cffi 一起约 1.6MB；av 要 128MB、imageio_ffmpeg 要 84MB，都不划算。
    # 注意：**必须加在这里**。backend_server.spec 是 PyInstaller 每次重写的产物，
    # 手改那个文件下一次打包就被冲掉了（实测踩过）。
    if _have_module("miniaudio"):
        hidden += ["miniaudio", "_miniaudio"]
        print("    真唱依赖 miniaudio 已就位，会一起打进包里")
    else:
        print("    （没装 miniaudio，打出来的包唱歌只会念、不会唱；聊天不受影响）")
    for h in hidden:
        cmd += ["--hidden-import", h]
    for e in EXCLUDE:
        cmd += ["--exclude-module", e]
    # v1.9.0（模块四）：安全模块用编译产物顶替源码。
    # 必须同时 --exclude-module，否则 PyInstaller 会把 audit.py 也塞进 PYZ，
    # 源码照样能被抠出来，编译就白做了。
    step_build_secret()
    _pyds = step_cython()
    if _pyds:
        for _p in _pyds:
            _m = os.path.splitext(os.path.basename(_p))[0]
            cmd += ["--add-binary", _p + os.pathsep + "."]
            cmd += ["--exclude-module", _m]
        print("    已用 .pyd 顶替：%s（源码不再进包）"
              % ", ".join(os.path.splitext(os.path.basename(x))[0] for x in _pyds))
    # 任务一（V1.0.0-Beta）：把本地向量模型一起打进去。
    # 打了就不用联网去 HuggingFace 下模型；用户机器上开箱就能做向量检索。
    # 找不到就跳过（代码里有降级：没有模型就退关键词+时间戳，不会崩）。
    model_dir = os.path.join(HERE, "models")
    if os.path.isdir(model_dir) and _dir_has_files(model_dir):
        cmd += ["--add-data", model_dir + os.pathsep + "models"]
        print("    已带上本地向量模型：%s" % _dir_size_mb(model_dir))
    else:
        print("    （没找到 models/ 目录，跳过本地向量模型；运行时会走降级）")
    # 第四阶段（V1.3.2）：音视频模块跟着一起走。
    # v1.3.2 起优先用独立打包的 av_server.exe（138MB，自带 Python 运行时）：
    #   * Tauri 会把 resources/av_module 整目录带进安装包，av_bridge 优先找 exe 拉起；
    #   * 如果这里再把它 --add-data 进 backend exe，等于同一个 138MB 在安装包里躺两份，
    #     所以只有"还没打 exe"时才退回把源码塞进 backend（老的 python 启动方式）。
    av_dir = os.path.join(HERE, "resources", "av_module")
    av_exe = os.path.join(av_dir, "av_server.exe" if os.name == "nt" else "av_server")
    if os.path.isfile(av_exe):
        print("    音视频模块已独立打包：%s（%.1f MB，不重复塞进后端 exe）"
              % (os.path.basename(av_exe), os.path.getsize(av_exe) / 1048576.0))
    elif os.path.isdir(av_dir) and _dir_has_files(av_dir):
        cmd += ["--add-data", av_dir + os.pathsep + "av_module"]
        print("    已带上音视频模块源码：%s" % _dir_size_mb(av_dir))
        print("    提示：跑 `python build_av.py` 可以打成 av_server.exe，用户就不需要装 Python")
    else:
        print("    （没找到 resources/av_module，跳过音视频模块；界面会提示“没有音视频模块”）")
    # v1.8.16：langs/ 必须进包。以前漏了这一步，PyInstaller 环境下 _MEIPASS/langs
    # 不存在，界面语言切 English 后整页仍是中文（前端拿到的 UI_MAP 是空字典）。
    # 词表本身还有 i18n_builtin_en.py 兜底，这里是双保险，也让用户能改文件。
    langs_dir = os.path.join(HERE, "langs")
    if os.path.isdir(langs_dir) and _dir_has_files(langs_dir):
        cmd += ["--add-data", langs_dir + os.pathsep + "langs"]
        print("    已带上界面语言包：%s" % _dir_size_mb(langs_dir))
    else:
        print("    （没找到 langs/，界面语言将只用内置词表）")
    cmd.append(os.path.join(HERE, "backend_server.py"))

    if run(cmd) != 0:
        print("！！打包失败，看上面的报错")
        return 4

    exe = os.path.join(DIST, EXE_NAME + (".exe" if os.name == "nt" else ""))
    if not os.path.isfile(exe):
        print("！！没看到产物：" + exe)
        return 5
    print("[3/3] 产物：" + exe + "（%.1f MB）" % (os.path.getsize(exe) / 1048576.0))

    if not a.no_copy:
        os.makedirs(RES_DIR, exist_ok=True)
        dst = os.path.join(RES_DIR, os.path.basename(exe))
        shutil.copy2(exe, dst)
        print("已复制到：" + dst)

    # v1.9.1：BUILD_SECRET 只该活在产物里。以前这个清理函数只定义没调用，
    # 结果 _build_secret.py 留在源码树里，被打进了开源包（等于把盐的派生密钥
    # 一起发出去）。放在最后调用，覆盖正常路径。
    clean_build_secret()

    print("\n下一步：cd desktop && npm run tauri build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
