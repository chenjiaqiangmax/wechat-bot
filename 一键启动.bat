@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 大肥鱼 - 一键启动

echo ============================================
echo   大肥鱼微信机器人  一键启动
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

tasklist /fi "imagename eq Weixin.exe" 2>nul | findstr /i "Weixin.exe" >nul
if errorlevel 1 (
    echo [警告] 没检测到 Weixin.exe，机器人能启动但收发消息会失败
    echo 请先登录微信
    echo.
)

echo.
rem Restart semantics: stop any bot that is still running, so a second
rem double-click starts a fresh one instead of printing "already running".
rem Keep every echo here at top level (a Chinese echo inside if(...) gets
rem split by cmd and run as a command). This runs once, before :botloop;
rem the watchdog restart path jumps straight back there and skips it.
python "tools\stop_running.py"
if errorlevel 1 goto stopwarn
goto afterstop

:stopwarn
echo [警告] 旧实例没能全部停掉，继续启动可能两个进程抢微信窗口

:afterstop
netstat -ano 2>nul | findstr ":8765" | findstr "LISTENING" >nul
if errorlevel 1 (
    rem Run the backend with pythonw so no second console window pops up.
    rem Fall back to a minimized window only when pythonw is missing.
    where pythonw >nul 2>nul
    if errorlevel 1 (
        start /min "大肥鱼-后台" cmd /c python -m admin
    ) else (
        start /b "" pythonw -m admin >> admin.log 2>&1
    )
    echo [后台] 已在后台启动（无窗口），地址 http://127.0.0.1:8765
) else (
    echo [后台] 8765 已在监听，跳过启动
)

echo.
:botloop
echo [机器人] 启动中，日志显示在这个窗口，Ctrl+C 或关闭窗口即停止
echo.
python dafeiyu_bot.py
set "RC=%errorlevel%"
rem 75 = watchdog decided the send hung and exited on purpose.
rem See WATCHDOG_EXIT_CODE in dafeiyu_bot.py. Only 75 auto-restarts;
rem anything else is Ctrl+C or a real error, so fall through to pause.
rem Keep branch echo lines outside if(...) blocks, and keep every rem line
rem ASCII-only: cmd decodes this UTF-8 file with the ANSI codepage and will
rem split a Chinese rem into pieces it then runs as commands.
if "%RC%"=="75" goto watchdog_restart
echo.
echo [机器人] 已退出，错误码 %RC%
pause
exit /b %RC%

:watchdog_restart
echo.
echo [看门狗] 发送卡住超过上限，机器人已退出；3 秒后自动重启
timeout /t 3 /nobreak >nul
goto botloop

:nopython
echo [错误] 找不到 python
echo 请安装 64 位 Python 3.9+，安装时勾选 Add python.exe to PATH
pause
exit /b 1
