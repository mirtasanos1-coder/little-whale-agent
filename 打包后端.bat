@echo off
chcp 936 >nul
cd /d "%~dp0"
title 小鲸鱼 - 打包 Python 后端
echo ============================================================
echo  第 1 步 / 共 2 步：把 Python 后端打成 backend_server.exe
echo.
echo  产物：dist\backend_server.exe
echo        并自动复制到 desktop\src-tauri\resources\
echo  说明：用户机器上不一定装了 Python，所以发布版必须是自带解释器的 exe。
echo ============================================================
echo.
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY goto nopython
%PY% "%~dp0build_backend.py"
if errorlevel 1 goto failed
echo.
echo 已生成 backend_server.exe。
echo 接着双击 打包桌面端.bat 就能出安装包。
pause
exit /b 0

:nopython
echo 没找到 python。请先装 Python 3.11+（安装时勾选 Add Python to PATH）。
pause
exit /b 1

:failed
echo.
echo 打包失败，请看上面的报错。
pause
exit /b 1