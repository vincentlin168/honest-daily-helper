@echo off
REM ============================================================
REM  诚实日报助手 · 一键打包
REM  产出：app\dist\HonestDailyHelper.exe（单文件，双击即用）
REM  前置：pip install pyinstaller
REM ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul || (echo [错误] 未找到 Python。 & pause & exit /b 1)

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo 正在安装 PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 (echo [错误] PyInstaller 安装失败。 & pause & exit /b 1)
)

echo 正在打包...
python -m PyInstaller HonestDailyHelper.spec --noconfirm --clean
if errorlevel 1 (echo [错误] 打包失败。 & pause & exit /b 1)

echo.
echo 打包完成：%CD%\dist\HonestDailyHelper.exe
echo.
echo 把 dist 目录整个复制到目标机器即可运行：
echo   1. 双击 HonestDailyHelper.exe（首次会在同目录生成 config.json）
echo   2. 编辑 config.json 填入 API Key
echo   3. 双击「启动诚实日报助手.bat」打开面板
pause
endlocal
