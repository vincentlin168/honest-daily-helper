# -*- mode: python ; coding: utf-8 -*-
# 诚实日报助手 · PyInstaller 打包配置
#
# 用法：python -m PyInstaller HonestDailyHelper.spec
# 产出：dist/HonestDailyHelper.exe（单文件，双击即用）

import os

# 资源目录：打包时以脚本所在目录为基准（spec 文件与 main.py 同级）
BASE = os.path.abspath(os.getcwd())

a = Analysis(
    ['main.py'],
    pathex=[BASE],
    binaries=[],
    # 必须包含的运行时资源：
    #   web_ui.html          —— Web UI 页面（SkillServer 用 _resource_path 读取）
    #   config.example.json  —— 首次运行时据此生成默认 config.json
    datas=[
        (os.path.join(BASE, 'web_ui.html'), '.'),
        (os.path.join(BASE, 'config.example.json'), '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HonestDailyHelper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 窗口模式：双击后不弹黑色控制台窗口（配合托盘/启动器更干净）
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
