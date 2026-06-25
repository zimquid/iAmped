# -*- mode: python ; coding: utf-8 -*-
#
# Platform-adaptive PyInstaller spec for the portable iAmped app.
#   macOS  -> iAmped.app    (onedir + .app bundle)
#   Win    -> iAmped.exe    (onefile)
#   Linux  -> iAmped        (onefile)
#
# The single-page web UI (iamped/web/) is bundled as package data so the frozen
# app serves it from the extracted bundle. Plex/HTTP/desktop-window deps are
# collected wholesale because pywebview and plexapi load backends dynamically.
#
# Build:  pyinstaller --noconfirm build_tools/iamped.spec
import os, sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

IS_MAC = sys.platform == "darwin"

ROOT = os.path.abspath(os.getcwd())

# Bundle the SPA at iamped/web so server.py's WEB_DIR resolves inside the app.
datas = [(os.path.join(ROOT, "iamped", "web"), os.path.join("iamped", "web"))]
binaries = []
hiddenimports = collect_submodules("iamped")

# These packages load GUI, parser, image-codec, and tag backends dynamically.
for pkg in ("webview", "plexapi", "PIL", "mutagen"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # noqa: BLE001
        print("WARNING: could not collect %s (%s)" % (pkg, exc))

a = Analysis(
    [os.path.join(ROOT, "iamped_main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if IS_MAC:
    # onedir + BUNDLE => iAmped.app
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="iAmped",
              console=False, disable_windowed_traceback=False)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="iAmped")
    app = BUNDLE(
        coll,
        name="iAmped.app",
        icon=None,
        bundle_identifier="com.iamped.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleShortVersionString": "0.1.0",
        },
    )
else:
    # Windows / Linux: single self-contained executable.
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="iAmped",
        console=False,
        onefile=True,
        disable_windowed_traceback=False,
        strip=False, upx=False,
    )
