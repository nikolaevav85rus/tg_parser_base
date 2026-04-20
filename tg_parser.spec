# -*- mode: python ; coding: utf-8 -*-
# Сборка: pyinstaller tg_parser.spec

block_cipher = None

a = Analysis(
    ['app_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('icon.png', '.'),
        ('icon.ico', '.'),
    ],
    hiddenimports=[
        'aiosqlite', 'aiohttp', 'aiofiles',
        'telethon', 'pybit', 'fastapi', 'uvicorn',
        'jinja2', 'dotenv',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='TgParserBot',
    debug=False,
    strip=False,
    upx=False,
    console=False,          # без терминального окна
    icon='icon.ico',
)
