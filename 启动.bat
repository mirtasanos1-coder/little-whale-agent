@echo off
cd /d "%~dp0"
title 小鲸鱼 Agent 控制台

rem v1.2.1：界面在 Tauri（desktop\），Python 只是后端。
rem 这里按「安装版 -> 本地构建 -> 源码兜底」的顺序找桌面端。

if exist "%~dp0小鲸鱼.exe" goto runinstalled
if exist "%~dp0desktop\src-tauri\target\release\小鲸鱼.exe" goto runrelease
if exist "%~dp0desktop\src-tauri\target\debug\小鲸鱼.exe" goto rundebug
if exist "%~dp0desktop\src-tauri\target\debug\desktop.exe" goto rundebug
goto runsrc

:runinstalled
start "" "%~dp0小鲸鱼.exe"
goto done

:runrelease
start "" "%~dp0desktop\src-tauri\target\release\小鲸鱼.exe"
goto done

:rundebug
start "" "%~dp0desktop\src-tauri\target\debug\desktop.exe"
goto done

:runsrc
echo 没找到桌面端，改用「后端 + 浏览器」方式启动...
set "PY="
where python >nul 2>nul && set "PY=python"
where py >nul 2>nul && if not defined PY set "PY=py"
if not defined PY goto nopython
start "" http://127.0.0.1:5701/
%PY% "%~dp0backend_server.py"
goto done

:nopython
echo 没找到 Python，也没找到桌面端。
echo 请先运行「打包桌面端.bat」生成桌面程序，或安装 Python 3.11+。
pause

:done