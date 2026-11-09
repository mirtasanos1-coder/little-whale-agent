@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   模拟音视频通话模块  ^|  AV Module
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python，请先安装 Python 3.10+ 并勾选 "Add to PATH"
  pause & exit /b 1
)

if "%AV_PORT%"=="" set AV_PORT=5800

rem ---- 首次运行自动装依赖（存在 sentinel 才跳过）----
if not exist "runtime\.deps_ok" (
  echo [1/3] 首次运行：安装依赖（国内走清华镜像，约 260MB，可 Ctrl+C 取消）
  python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo [警告] 依赖安装有失败项，稍后可能影响部分功能。
  ) else (
    if not exist "runtime" mkdir runtime
    echo ok> "runtime\.deps_ok"
  )
) else (
  echo [1/3] 依赖已安装，跳过 pip
)

echo [2/3] 检查虚拟驱动 ...
python install_drivers.py --check
echo.
echo   如需安装/补齐： python install_drivers.py --install     （会弹 UAC）
echo.

echo [3/3] 启动服务: http://127.0.0.1:%AV_PORT%  （启动后会自动打开浏览器）
python av_server.py --port %AV_PORT% --open

echo.
echo 服务已退出。
pause
