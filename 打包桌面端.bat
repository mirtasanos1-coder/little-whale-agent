@echo off
chcp 936 >nul
set "ROOT=%~dp0"
cd /d "%ROOT%desktop"
title 小鲸鱼 - 打包桌面端 (Tauri)
echo ============================================================
echo  第 2 步 / 共 2 步：打包 Tauri 桌面端
echo.
echo  前置：先跑过 打包后端.bat，确保
echo        desktop\src-tauri\resources\backend_server.exe 存在。
echo  产物：desktop\src-tauri\target\release\bundle\
echo ============================================================
echo.

rem ---- 让 npm / cargo 一定能找到（从桌面双击时 PATH 常常不全）----
if exist "C:\Program Files\nodejs\npm.cmd" set "PATH=C:\Program Files\nodejs;%PATH%"
if exist "%USERPROFILE%\.cargo\bin" set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"

if exist "src-tauri\resources\backend_server.exe" goto build

echo ！！还没打后端，desktop\src-tauri\resources\backend_server.exe 不存在。
echo    请先双击 打包后端.bat，跑完再回来。
pause
exit /b 1

:build
if not exist "node_modules" (
  echo 首次打包，先装前端依赖（用 npmmirror 镜像，国内快）...
  call npm install --registry=https://registry.npmmirror.com --no-audit --no-fund
  if errorlevel 1 goto failed
)

echo 开始编译（第一次要下载并编译 Rust 依赖，五到十五分钟，别关窗口）...
echo.
call npm run tauri build
if errorlevel 1 goto failed

echo.
echo 打包完成，安装包在 desktop\src-tauri\target\release\bundle\ 下面。
pause
exit /b 0

:failed
echo.
echo 打包失败，请看上面的报错。
pause
exit /b 1