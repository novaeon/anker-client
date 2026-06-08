# AnkerClient

AnkerClient is an unofficial desktop client frontend for AnkerGames.

It provides a Windows desktop UI for browsing, downloading, installing, launching, and managing games from AnkerGames. Built EXEs are published through GitHub Releases and are not committed to this repository.

## Features

- Login with saved credentials through the Windows keyring.
- Search and browse game details.
- Download and install games into a local library folder.
- Detect likely game executables after extraction.
- Create Desktop and Start Menu shortcuts.
- Cache local library metadata and cover images.
- Switch between bundled themes, including Bliss XP and Vaporwave.

## Requirements

- Windows
- Python 3.12 or newer
- 7-Zip installed and configured in the app settings

## Development Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest -q
python -m anker_client.main
```

## Build

```powershell
python -m pip install -e ".[build]"
.\build.bat
```

The Windows executable is written to `dist\AnkerClient.exe`.

## Releases

Release builds are produced by GitHub Actions when a tag matching `v*` is pushed. See [docs/releasing.md](docs/releasing.md).

## Disclaimer

This project is unofficial and is not affiliated with, endorsed by, or sponsored by AnkerGames. Use it with your own account and follow the AnkerGames terms and applicable law.
