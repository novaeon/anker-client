# Contributing

Thanks for helping improve AnkerClient. Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):
it explains the layers, the threading model, the AnkerGames site protocol and
where everything is stored.

## Setup

Windows 10/11 and Python 3.12 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

7-Zip is recommended for running the app (zip archives also work without it).

## Run

```powershell
python -m anker_client            # the real app
python -m anker_client --debug    # verbose logging to the console and log file
python -m tests.fakes             # the real UI on fake data (no network, no installs)
```

`ANKERCLIENT_HOME=<folder>` keeps all settings, caches and the database in that
folder instead of `%APPDATA%`/`%LOCALAPPDATA%` — handy for a throwaway profile.

## Checks

```powershell
python -m ruff check anker_client tests packaging
python -m pytest -q
```

* `tests/unit` — pure logic (parsers run against real pages saved in `tests/fixtures/site`).
* `tests/gui` — pytest-qt, offscreen, on `tests.fakes.FakeContext`. GUI tests also write
  screenshots to `build/screens/` — look at them when you change the UI.
* `tests/live` — talk to the real website; opt in with `$env:ANKER_LIVE_TESTS = "1"`.
  Run them when the site changes; refresh the fixtures afterwards.

## Guidelines

* Respect the layer rule: `core` ← `site` ← `services` ← `ui`. Only `ui` imports Qt.
* Never block the GUI thread; use `ui.async_.run_async`.
* Long operations take a `token: CancelToken` and check it between blocking steps.
* Raise `core.errors` exceptions across layers, with messages a user can act on.
* Colours come from the theme palette, icons from `ui.icons`.
* New behaviour needs tests. Bug fixes need a test that fails without the fix.

## Repository hygiene

* Commit source, tests, docs and workflow files only — never `build/`, `dist/`, logs or EXEs.
* Fixtures must not contain personal data (IP addresses, session cookies, signed links).
