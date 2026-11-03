# PyInstaller spec untuk Rapix (macOS dan Windows).
# Dipanggil oleh build_mac.sh / build_windows.bat; tidak perlu dijalankan langsung.
import sys
from pathlib import Path

HERE = Path(SPECPATH)
ROOT = HERE.parent
IS_MAC = sys.platform == "darwin"
VERSION = "1.6.0"

binaries = [(str(ROOT / "virtualdisplay"), "."), (str(ROOT / "screenstream"), ".")] if IS_MAC else []
icon = str(HERE / "build" / ("icon.icns" if IS_MAC else "icon.ico"))

a = Analysis(
    [str(ROOT / "server.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[(str(ROOT / "static"), "static")] + ([(str(HERE / "build" / "menubar.png"), ".")] if IS_MAC else []),
    hiddenimports=["qrcode.image.svg"] + (["Quartz", "HIServices", "AppKit", "Foundation", "WebKit"] if IS_MAC else []),
    excludes=["tkinter", "mouseinfo"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Rapix",
    console=False,  # tanpa jendela terminal; status tampil di panel browser
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Rapix")

if IS_MAC:
    app = BUNDLE(
        coll,
        name="Rapix.app",
        icon=icon,
        bundle_identifier="id.rap.rapix",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "Rapix",
            "CFBundleName": "Rapix",
            "NSHumanReadableCopyright": "© 2026 RAP",
            "CFBundleShortVersionString": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "LSUIElement": True,  # tidak muncul di Dock; kendali lewat panel di browser
            "NSHighResolutionCapable": True,
        },
    )
