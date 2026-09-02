from pathlib import Path


project_root = Path(SPECPATH).resolve()
icon_path = project_root / "assets" / "fysio.ico"
demo_path = project_root / "data" / "physio_new.db"

if not icon_path.is_file():
    raise SystemExit(f"Missing Windows icon: {icon_path}")
if not demo_path.is_file():
    raise SystemExit(f"Missing packaged Demo database: {demo_path}")

datas = [
    (str(project_root / "templates"), "templates"),
    (str(project_root / "static"), "static"),
    (str(demo_path), "resources"),
]

a = Analysis(
    [str(project_root / "fysio_launcher.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fysio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(icon_path),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Fysio",
)
