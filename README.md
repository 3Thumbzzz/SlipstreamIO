# SlipStreamIO

Keeps DJ libraries in sync: playlists, cue points, beatgrids and track tags
between **Traktor**, **VirtualDJ**, **Rekordbox** (XML) and **Serato DJ**, with a
side-by-side view of what differs and why.

## Download

Get the latest build from [Releases](../../releases):

- **Windows (x64):** unzip `SlipStreamIO-windows-x64.zip` anywhere and run
  `SlipStreamIO.exe`. Settings, backups and logs are kept in that folder.
- **macOS (Apple silicon):** unzip `SlipStreamIO-macos-arm64.zip` and move
  `SlipStreamIO.app` to Applications. The app is not signed: the first time,
  right-click it and choose **Open**. Settings, backups and logs are kept in
  `~/Library/Application Support/SlipStreamIO`.

Both need **ffmpeg** (with ffprobe) installed: on Windows on the `PATH`, on
macOS `brew install ffmpeg`.

Close Traktor, VirtualDJ, Rekordbox and Serato before syncing. They write their
own library back when they quit and would overwrite the changes.

## Run from source

Python 3.13:

```
pip install -r requirements.txt
python app.py
```

Tests: `pip install -r requirements-dev.txt`, then
`python -m pytest tests -n auto --dist loadfile`.

## Build

```
pip install -r requirements-dev.txt
pyinstaller SlipStreamIO.spec --noconfirm
```

This builds for the platform you run it on (`dist/SlipStreamIO/` on Windows,
`dist/SlipStreamIO.app` on macOS). Pushing a `v*` tag runs
`.github/workflows/release.yml`, which builds both and publishes a release.

## Documentation

[HANDOFF.md](HANDOFF.md) is the working guide to the codebase: how each
program stores its library, the timing rules for cues, and the safety rules
every write follows. [ARCHITECTURE.md](ARCHITECTURE.md) has the package map.
