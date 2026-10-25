# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the Windows build: one folder, one program (ArctisSoundManager.exe) that plays every role
# (tray, window, daemon, audio engine) according to its first argument - see scripts/winmain.py.
#
#   pyinstaller --noconfirm --distpath build\dist --workpath build\pyi installer\asm.spec
#
# The native helper (asm-audiotap.exe), the RNNoise library and the VB-CABLE driver pack are put next to it by
# installer\build.ps1; the spec only deals with Python.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

datas = collect_data_files("arctis_sound_manager")               # lang, devices, presets, hrir_assets, images ...
datas += copy_metadata("arctis-sound-manager")                   # project_version() reads it
binaries = []
hidden = collect_submodules("arctis_sound_manager")              # a lot is imported by name at run time
hidden += ["sounddevice", "hid", "babel.numbers", "babel.dates", "ruamel.yaml"]
hidden += collect_submodules("winrt")                            # the system media controls (the DAC's media line)

for pkg in ("sounddevice", "hid", "pyrnnoise"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass
try:
    datas += collect_data_files("sounddevice")
    datas += collect_data_files("babel")
except Exception:
    pass

# numba compiles the reverb / limiter / dynamics loops (20-100x faster than the numpy fallback). It needs the
# .py sources next to the compiled modules to cache its compilations, hence 'py+pyz' for our own package.
module_collection_mode = {"arctis_sound_manager": "py+pyz"}
try:
    import numba  # noqa: F401
    def _no_tests(name):                                         # their own test suites are most of what they would add
        return ".tests" not in name and not name.endswith(".tests")
    hidden += collect_submodules("numba", filter=_no_tests) + collect_submodules("llvmlite", filter=_no_tests)
    datas += collect_data_files("numba", excludes=["**/tests/**"])
    binaries += collect_dynamic_libs("llvmlite")
except Exception:
    pass

a = Analysis(
    [os.path.join(SRC, "arctis_sound_manager", "scripts", "winmain.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "pyudev", "IPython", "notebook", "PyQt5", "PyQt6", "PySide2",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore",
              "PySide6.QtQuick3D", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
              "numba.tests", "numba.cuda.tests", "llvmlite.tests", "scipy.tests", "numpy.tests", "numpy.testing.tests"],
    module_collection_mode=module_collection_mode,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ArctisSoundManager",
    debug=False,
    strip=False,
    upx=False,
    console=False,                       # no terminal window, ever (the daemon and the engine are this very program)
    icon=os.path.join(SPECPATH, "asm.ico") if os.path.exists(os.path.join(SPECPATH, "asm.ico")) else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ArctisSoundManager")
