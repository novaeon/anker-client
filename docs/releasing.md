# Releasing

AnkerClient releases are built by GitHub Actions from tags.

## Create a Release

1. Make sure `main` is clean and CI is passing.
2. Create and push a version tag:

```powershell
git tag v0.1.0
git push origin v0.1.0
```

3. The release workflow runs tests, builds `dist\AnkerClient.exe` with PyInstaller, and creates a GitHub Release for the tag.
4. Confirm the release contains `AnkerClient.exe`.

## Local Build Check

```powershell
python -m pip install -e ".[dev,build]"
python -m pytest -q
.\build.bat
```

The EXE should be substantially larger than the PyInstaller bootloader. A tiny output usually means the build failed or packaged incompletely.
