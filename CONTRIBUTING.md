# Contributing

Thanks for helping improve AnkerClient.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Checks

Run the test suite before opening a pull request:

```powershell
python -m pytest -q
```

For packaging changes, also run:

```powershell
python -m pip install -e ".[build]"
.\build.bat
```

## Repository Hygiene

- Commit source, tests, docs, and workflow files only.
- Do not commit generated folders such as `build/`, `dist/`, `logs/`, `.pytest_cache/`, or `__pycache__/`.
- Do not commit EXEs. Release binaries are attached to GitHub Releases.
- Keep public documentation user-facing and avoid local machine paths or private planning notes.
