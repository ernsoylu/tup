# PyInstaller spec for the tup single-file executable (CLI + GUI in one binary).
# Build locally or in CI with:  uv run pyinstaller packaging/tup.spec
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["entry.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    # tup.gui.* is imported lazily by the `tup gui` command, so static
    # analysis of the entry point alone would miss it.
    hiddenimports=collect_submodules("tup"),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="tup",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
