@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo.
echo   小鲸鱼 · 恢复官方安装（把程序文件换成官方原版，不动你的记忆和人设）
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0恢复官方安装.ps1" %*
echo.
pause
