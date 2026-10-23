# PyInstaller spec - build with:  pyinstaller packaging/PharmaTnT.spec --noconfirm
# Produces dist/PharmaTnT/ (Windows/Linux folder app) or dist/PharmaTnT.app (macOS).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICON = {"win32": "packaging/icon.ico", "darwin": "packaging/icon.icns"}.get(sys.platform)
ICON = str(ROOT / ICON) if ICON and (ROOT / ICON).exists() else None

a = Analysis(
    [str(ROOT / "packaging" / "desktop_main.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "tnt" / "static"), "tnt/static")],
    hiddenimports=collect_submodules("uvicorn") + ["tnt.api", "webview"],
    excludes=["pytest", "httpx", "playwright"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PharmaTnT", console=False, icon=ICON)
coll = COLLECT(exe, a.binaries, a.datas, name="PharmaTnT")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="PharmaTnT.app", icon=ICON, bundle_identifier="com.pharmatnt.desktop",
                 info_plist={"CFBundleDisplayName": "Pharma Track & Trace", "NSHighResolutionCapable": True,
                             "CFBundleShortVersionString": "2.1.0"})
