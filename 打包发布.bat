@echo off
chcp 936 >nul
cd /d "%~dp0"
title 小鲸鱼 - 一键出包（安装版 + 绿色版）
echo.
echo  这一步会做四件事：
echo    1) 检查/下载 NapCat 离线包（约 118MB，已缓存就跳过）
echo    2) 打 backend_server.exe（含语音引擎）
echo    3) 打 Tauri 安装包（首次编译约 3~5 分钟）
echo    4) 出 release\小鲸鱼_x64-setup.exe 和 release\小鲸鱼_V..._绿色版.zip
echo.
echo  打完把 release\ 里两个文件发出去就行。按 Ctrl+C 可以中断。
echo.
pause
set "PY="
where python >nul 2>nul && set "PY=python"
where py >nul 2>nul && if not defined PY set "PY=py"
if not defined PY goto nopython
%PY% "%~dp0打包发布.py" %*
if errorlevel 1 goto failed
echo.
echo 出包完成，去 release\ 里看看。
pause
exit /b 0
:nopython
echo 没找到 python，请先安装 Python 3.10 以上并勾选 "Add to PATH"。
pause
exit /b 1
:failed
echo.
echo 出包失败，往上翻看红色报错。常见原因：
echo   - 没装 Node.js / Rust（看 Docs\开发者指南.md）
echo   - 端口/文件被占用（先关掉正在运行的小鲸鱼）
pause
exit /b 1
