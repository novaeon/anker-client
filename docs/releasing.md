# Releasing

Releases are built by GitHub Actions when a `v*` tag is pushed.

1. Update `anker_client/__init__.py` (`__version__`) and `CHANGELOG.md`.
2. Make sure `main` is green in CI.
3. Tag and push:

   ```powershell
   git tag v1.0.0
   git push origin v1.0.0
   ```

4. The release workflow checks that the tag matches `__version__`, runs lint and
   tests, builds the one-folder app with PyInstaller, packs a portable zip,
   builds the Inno Setup installer and publishes both to a GitHub Release.

The app checks the latest GitHub release on startup (when enabled in Settings)
and tells users about new versions.

## Local build

```powershell
python -m pip install -e ".[dev,build]"
./scripts/build.ps1            # add -SkipTests / -SkipInstaller as needed
```

Outputs:

* `dist/AnkerClient/AnkerClient.exe` — the app (one-folder build; QtWebEngine
  needs its helper process and resources next to the exe)
* `dist/AnkerClient-<version>-portable.zip`
* `dist/AnkerClient-<version>-setup.exe` — when Inno Setup 6 is installed

Smoke test: `dist\AnkerClient\AnkerClient.exe --version`, then start it with a
throwaway profile (`$env:ANKERCLIENT_HOME = "$env:TEMP\ankertest"`).
