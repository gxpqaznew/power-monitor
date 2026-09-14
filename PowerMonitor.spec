# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、纯 Win32（不含 Qt）。"""

a = Analysis(
    ['run.pyw'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['powermon'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 整套界面都是 user32/gdi32 自绘，这些大块头一个都用不上
        'PySide6', 'PyQt5', 'PyQt6', 'shiboken6',
        'PIL', 'numpy', 'matplotlib', 'scipy', 'pandas',
        # 采集全部走 ctypes 直连系统 DLL，psutil 只在源码运行时作兜底
        'psutil',
        'tkinter', 'unittest', 'pydoc', 'doctest', 'test',
        'setuptools', 'pip', 'wheel',
    ],
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
    name='能耗统计',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
