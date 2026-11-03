# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for SlipStreamIO.

    pyinstaller SlipStreamIO.spec --noconfirm

Windows: dist/SlipStreamIO/SlipStreamIO.exe, a portable folder (settings,
backups and logs are written beside the exe).
macOS:   dist/SlipStreamIO.app (settings, backups and logs go to
~/Library/Application Support/SlipStreamIO; see config/paths.app_dir).

The images ship inside the package; an Images folder placed beside the app
overrides them (config/paths.images_dir). ffmpeg and ffprobe are not bundled:
they must be installed and on the PATH.
"""
import glob
import os
import sys

sys.path.insert(0, SPECPATH)
from trackcleaner import __version__  # noqa: E402

ICON = os.path.join(SPECPATH, "Images", "Slipstream.png")   # Pillow converts it
IMAGES = [(path, "Images")
          for path in glob.glob(os.path.join(SPECPATH, "Images", "*"))
          if os.path.splitext(path)[1].lower() in (".png", ".gif", ".jpg", ".svg")]

a = Analysis(
    ["app.py"],
    pathex=[SPECPATH],
    datas=IMAGES,
    excludes=["tkinter", "pytest", "_pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SlipStreamIO",
    console=False,
    icon=ICON,
)

coll = COLLECT(exe, a.binaries, a.datas, name="SlipStreamIO")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="SlipStreamIO.app",
        icon=ICON,
        bundle_identifier="io.github.3thumbzzz.slipstreamio",
        info_plist={
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
        },
    )
