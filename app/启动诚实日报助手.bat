@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  诚实日报助手 · 启动器
REM  以 Edge/Chrome 的「应用模式」打开本地面板 —— 无地址栏、
REM  无标签页，看起来就是一个独立的桌面应用。零额外依赖。
REM ============================================================
setlocal
cd /d "%~dp0"

set PORT=8088

REM --- 找 Python（优先 pythonw：无控制台窗口）---
set PY=
where pythonw >nul 2>nul && set PY=pythonw
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
    echo [错误] 未找到 Python，请先安装 Python 3.11+ 并加入 PATH。
    pause
    exit /b 1
)

REM --- 启动后台服务 ---
start "" /min %PY% main.py

REM --- 等待服务就绪 ---
REM     调用独立的 .ps1 探测脚本：cmd.exe 对内联 PowerShell 的转义过于脆弱，
REM     单行命令里的 $ 变量与管道符会被吃掉，导致永远探测不到服务。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0等待服务就绪.ps1" -Port %PORT% -TimeoutSeconds 30
if errorlevel 1 (
    echo [错误] 后台服务 30 秒内未就绪。请查看同目录的 honest-helper.log。
    pause
    exit /b 1
)

echo 服务已就绪，正在打开面板...

REM --- 找浏览器（Edge 优先，其次 Chrome）---
set BROWSER=
for %%P in (
    "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
    "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
    "%LocalAppData%\Microsoft\Edge\Application\msedge.exe"
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do (
    if exist %%P if not defined BROWSER set BROWSER=%%~P
)

if not defined BROWSER (
    echo 未找到 Edge / Chrome，改用默认浏览器打开。
    start "" "http://127.0.0.1:%PORT%"
    goto done
)

REM --- 应用模式（--app = 无地址栏、无标签页的独立窗口）---
start "" "%BROWSER%" --app="http://127.0.0.1:%PORT%" --window-size=1200,860

:done
echo.
echo 面板已打开。关闭窗口不会停止后台服务。
echo 如需停止：请在任务管理器结束 pythonw.exe 进程。
endlocal
