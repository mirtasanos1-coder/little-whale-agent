@echo off
cd /d "%~dp0"
title 小鲸鱼 控制台 - 网页版（调试用）

echo 这个是给开发/排错用的：只起后端，然后用浏览器打开界面。
echo 正常使用请直接双击「启动.bat」或桌面图标。
echo 关掉这个黑窗口 = 关掉后端。
echo.

set "PY="
where python >nul 2>nul && set "PY=python"
where py >nul 2>nul && if not defined PY set "PY=py"
if not defined PY goto nopython

echo 界面地址：http://127.0.0.1:5701/
%PY% "%~dp0backend_server.py"
if errorlevel 1 goto failed
goto done

:nopython
echo 没找到 Python。装好 Python 3.11+ 之后再来（或者直接用安装版）。
pause
goto done

:failed
echo 启动失败。常见原因：端口被占用（先关掉旧的黑窗口再试）。
pause

:done