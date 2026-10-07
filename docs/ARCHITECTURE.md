# AnkerClient architecture

AnkerClient is a Windows desktop client and download manager for AnkerGames
(ankergames.net): browse and search the store, download games with a
resumable multi-connection downloader, install them (7-Zip), keep a library
with playtime tracking, detect game updates, and launch games.

Python ≥ 3.12, PyQt6 (+ QtWebEngine), requests, BeautifulSoup/lxml, SQLite,
keyring, psutil, pywin32.

## Layers

```
anker_client/
  constants.py            app-wide constants (no imports of other layers)
  core/                   Qt-free foundation: models, errors, events, settings, db, tasks, paths, formatting
  site/                   ankergames.net adapter: http, parsers (pure), livewire, client
  services/               business logic (Qt-free): catalog, images, auth, library, launcher, updates,
    downloads/            ratelimit, engine (resumable HTTP), resolver (ticket → file URL), manager (queue)
    install/              sevenzip, extractor, executables, diskspace, shortcuts, installer
    system/               autostart
    container.py          composition root → AppContext
  ui/                     PyQt6 only here
    bridge.py             EventBus → Qt signals on the GUI thread
    async_.py             run_async(): run service calls off the GUI thread
    image_loader.py       async image loading/decoding + LRU
    icons.py              inline SVG icon set
    theme/                palette tokens + ThemeManager (QSS)
    widgets/              shared widgets (common, cover_grid, image_label, flow_layout, …)
    pages/                store, game, library, downloads, settings
    dialogs/              login, verification (WebEngine), first_run, install_dialog, game_dialogs
    main_window.py        shell + Navigator implementation
  app.py                  entry point
```

Dependency rule: `core` ← `site` ← `services` ← `ui`. Nothing below `ui`
imports Qt. `services` never import each other in cycles; the container wires
them. Pages never import each other; they talk through `Navigator`.

## Threading model

* GUI thread: widgets only. Never block it (no network, no disk scans, no
  `subprocess.run`, no `time.sleep`).
* `TaskRunner` (8 workers): short/medium jobs. UI uses `ui.async_.run_async`.
  Every callable takes a keyword `token: CancelToken`.
* Dedicated threads: download scheduler + one thread per active job, segment
  connection threads inside the engine, the launcher's process monitor.
* Services publish immutable events on the `EventBus` from any thread; the UI
  listens via `QtEventBridge` signals (always delivered on the GUI thread).
* Shared state in services is guarded by locks; getters return copies.

## Errors

All cross-layer errors are `core.errors.AnkerError` subclasses with an
`ErrorKind` and a user-presentable `user_message`. Low-level exceptions are
wrapped at the boundary that understands them. The UI shows
`ui.async_.error_text(exc)`. Unexpected exceptions are logged with traceback
and surfaced as a generic message.

## Persistence

| What | Where |
|---|---|
| Settings | `%APPDATA%\AnkerClient\config.json` (`core.settings`) — migrated from legacy `settings.json` |
| SQLite (catalog, details cache, installs user data, play sessions, jobs, wishlist) | `%APPDATA%\AnkerClient\anker.db` |
| Session cookies (DPAPI-encrypted) | `%APPDATA%\AnkerClient\session.bin` |
| Credentials | Windows Credential Manager via `keyring` (service `AnkerClient`) |
| Image cache | `%LOCALAPPDATA%\AnkerClient\Cache\images` |
| Logs | `%LOCALAPPDATA%\AnkerClient\Logs\ankerclient.log` (rotating, secrets scrubbed) |
| Verification browser profile | `%LOCALAPPDATA%\AnkerClient\WebEngine` |
| Per-game manifest | `<game dir>\.ankerclient.json` (hidden) — source of truth for installs |
| Downloads in progress | `<library>\.ankerclient\downloads\<job id>\<file>.part` + `.part.json` |

`ANKERCLIENT_HOME=<dir>` relocates everything (tests, portable use).

## Site protocol (verified 2026-10-06)

See the docstrings of `site/parsers.py` and `site/client.py` for markup and
endpoint details. Summary:

* Listings: `GET /games?page=N&sort=K` (K ∈ created_at, view, like_count,
  vote_average, release_date, title), `/genre/{slug}?page&sort`, `/games/vr`,
  `/search/{q}?page=N`, `/top-games`, home sections on `/`. ~56 cards/page,
  ~37 pages total. Pages past the end → 404.
* Details: `/game/{slug}` JSON-LD (VideoGame) + download-option modal.
* Download: `POST /generate-download-url/{id}` (CSRF from `GET /csrf-token`)
  → ticket page `/download/{signed}/{hash}` → `downloadPage(fileUrl, provider,
  isTorrent, status, statusUrl, waitSeconds)` → `GET /download-file/{ticket}`
  (+ `?cf-turnstile-response=` when a Cloudflare Turnstile widget is on the
  page) → 302 to a CDN URL supporting Range/ETag.
* Turnstile must be completed by a real browser. AnkerClient never tries to
  solve or bypass it: it shows the site's own page in an embedded Chromium
  (QtWebEngine) where Cloudflare runs its normal check (usually invisible,
  sometimes a click). When the page itself starts the file download, the
  client captures the final URL from `QWebEngineProfile.downloadRequested`,
  cancels the browser download, and hands the URL to its own downloader. If
  QtWebEngine is unavailable, the user gets "Open in browser" + "Import
  archive" instead.
* The verification dialog shows only the page's Turnstile widget by default
  (compact card): isolated-world scripts hide the rest of the page and centre
  the real widget, which is never covered or modified. It falls back to the
  full page when no widget appears, on a site error, for popups, or on
  "Show full page" (see `ui/dialogs/verification.py`).

## Download → install pipeline

`DownloadManager` (persistent queue) → `LinkResolver` (ticket + verification)
→ `HttpDownloader` (segmented, resumable, rate-limited) → `Installer`
(7-Zip extract to staging on the target volume, find game root, atomic move,
manifest, shortcuts) → `LibraryService.register_install`. States and
retry/backoff rules are documented in `services/downloads/manager.py`.

Download options: FULL ("Direct…"), PATCH ("Update Only From V a To V b") and
ADDON ("Language Pack", "Launcher") — PATCH/ADDON extract over an existing
install.

## Startup sequence (`app.main`)

1. Parse args: `--minimized`, `--debug`, `--reset-window`, `--version`.
2. `AppPaths.default().ensure()`; `setup_logging` (level from settings or `--debug`).
3. Import `PyQt6.QtWebEngineWidgets` (guarded) **before** creating the
   `QApplication`; set `AA_ShareOpenGLContexts`; create `QApplication`
   (name/org/version/icon; `setQuitOnLastWindowClosed(False)`).
4. Single instance (`QLocalServer` named `AnkerClient-<user>`): a second
   launch sends `show` to the first instance and exits 0.
5. Install crash handlers (`sys.excepthook`, `threading.excepthook`): log,
   show an error dialog with "Copy details"/"Open logs" (GUI thread), never exit
   for exceptions in slots.
6. `build_context()`; `ThemeManager.apply(settings.theme)`; `QtEventBridge`;
   `ImageLoader`; set `HttpClient` UA to the embedded browser UA; create
   `WebEngineVerifier` and `ctx.resolver.set_verifier(verifier)`.
7. `MainWindow`; first-run wizard when `settings.first_run_completed` is
   False; show (or stay in tray when `--minimized`/`start_minimized`).
8. `ctx.start()`; background: legacy migration → library scan → auth restore
   → catalog sync when stale → image-cache prune; timers for game update checks
   (`game_update_interval_hours`) and app update check (startup + daily).
9. `app.exec()`; on quit: verifier shutdown, `ctx.shutdown()`, save window geometry.

## UI

General rules
* Use the palette (`ui.theme.palette.current()`) and QSS properties
  documented in `ui/theme/manager.py`; never hard-code colours.
* Icons via `ui.icons.icon(name)`; names listed in `ui/icons.py`.
* Every network/disk call through `run_async`; show loading (Spinner /
  LoadingOverlay), empty (EmptyState) and error states (message + Retry).
* Keep stale-result protection: cancel or ignore results of superseded requests.
* Update live from `QtEventBridge` signals; never poll services from a timer
  except for clocks (e.g. "Waiting 42s" countdowns).
* Pages are `QWidget` subclasses with `setProperty("role", "page")`, outer
  margins 24 px, section spacing 16–20 px. Window minimum size 1100×700.
* Text is plain and specific ("Install", "Downloading 45%", "Update to v1.2");
  every destructive action is confirmed and names the game.

Shell (`MainWindow`, implements `Navigator`)
* Native title bar. Left sidebar (role=sidebar, 220 px): logo + "AnkerClient",
  nav buttons (variant=nav, checkable, icons): Store, Library (count badge),
  Downloads (active count badge + thin aggregate progress), Settings at the
  bottom, and an account chip (initials avatar + name, or "Sign in") with a
  menu (Open profile on website, Sign out).
* Header row above the page stack: Back button (history), global search
  (role=search with search icon; Enter / 400 ms debounce → `show_store(query)`),
  right side: catalog-sync indicator and update notices (e.g. "3 updates" chip
  → Library filtered by updates).
* `QStackedWidget` with Store, Game, Library, Downloads, Settings pages.
* Bottom status strip (role=statusbar): download summary
  ("2 downloading · 12.3 MB/s · 4m left" / "Paused" / idle text), click → Downloads.
* Themes with a wallpaper paint it behind translucent panels (GIF via
  `QMovie`, throttled ~15 fps, paused while minimized).
* Toasts: in-window stack bottom-right, 4 s (errors 8 s), click to dismiss;
  also used for `Notification` events. Tray balloons for `tray=True`
  notifications when the window is hidden and notifications are enabled.
* Tray icon: tooltip with download progress; menu: Open AnkerClient, Recently
  played (up to 5 → launch), Pause all / Resume all downloads, Quit.
* Close: `close_behavior` ask (dialog with "Remember my choice") / tray / quit.
  Quitting with active downloads asks for confirmation ("Downloads will pause
  and resume next time").
* Shortcuts: Ctrl+F search, Ctrl+1..4 pages, Alt+Left back, F5 refresh page,
  Ctrl+Q quit.
* `request_install(details, option)`: `InstallDialog` (option, library,
  free-space check per volume, overwrite warning) → `ctx.downloads.enqueue`
  → toast "Added to downloads" with action. PATCH/ADDON need the base game
  installed (dialog explains otherwise).
* `choose_executable(install_id)`: `ExecutablePickerDialog`.
* On `GameInstalled(needs_executable=True)`: toast with "Choose executable".

Store page
* Header: title + tabs "Discover" | "Browse" | "Wishlist".
* Discover: vertical scroll of horizontal rows (home sections + "Top games");
  each row is a non-wrapping `CoverGridView` (fixed height) with "See all"
  where a browse equivalent exists. Cached ~10 minutes.
* Browse: sort combo (`SortOrder.label`), genre chips (variant=chip, from
  `client.genres()` + "VR"; hide NSFW unless `show_nsfw`), status line, cover
  grid with infinite scroll (`near_end` → next page), card badges: Installed
  (success), Update (warning), "Downloading 45%" + progress bar, favorite heart
  for wishlisted.
* Search (`set_query`): instant local results (`catalog.search`) merged with
  server results (`client.search`, paginated), de-duplicated; empty state.
* Wishlist tab: grid of wishlisted games; empty state with "Browse the store".
* Card context menu: View details, Add/Remove wishlist, Open on website, Copy link.
* Seen listing games are fed to `catalog.upsert` in the background.

Game page
* Hero banner (hero/first screenshot, scrim) with back button, title
  (role=display), meta line (year · size · version · "Updated 3 days ago"),
  genre chips.
* Main column: screenshot carousel (large image + thumbnails; click → lightbox
  dialog with arrow keys), About (description with "Show more"), System
  requirements card (OS, Processor, Memory, Graphics, DirectX, Storage).
* Side column: cover poster; action card whose primary button reflects state:
  Install (+ option menu for multiple options) / progress + Pause/Resume/Cancel
  while a job runs / Play (+ Manage menu) when installed / Update "v1 → v2"
  (patch when available) / Stop when running. Facts list (Size, Version,
  Released, Updated, Torrent requires sign-in). Wishlist toggle, Open on
  website, Copy link. Add-ons list (ADDON options) with Install buttons when
  the base game is installed.
* Immediate render from the summary/cache, then fresh details; live updates via bridge.

Library page
* Toolbar: title + count, filter box, filter combo (All, Favorites, Updates
  available, Needs setup, Unmanaged, Hidden), sort combo (Title, Recently
  played, Playtime, Recently installed, Size), grid/list toggle, "Add games"
  menu (Import archive…, Import existing folders…, Rescan libraries), "Check
  for updates".
* Grid (CoverGridView; badges Update / Running / Needs setup / Unmanaged;
  favorite heart) or list (sortable table: Title, Playtime, Last played, Size,
  Version, Status). Double-click plays.
* Detail panel (right, ~380 px): artwork, title, Play/Stop/Update button,
  playtime, last played, version, size (computed async), install folder link,
  genres; actions: Properties, Open folder, Favorite, Hide, Create shortcuts,
  Install prerequisites (when `has_redist and not redist_installed`), Check for
  update, Repair (reinstall), Store page, Uninstall (danger, confirmation names
  the game and its size).
* Empty state: "Your library is empty" + "Browse the store" + "Import existing games".

Downloads page
* Header: aggregate speed, Pause all / Resume all, Clear finished, speed-limit
  menu (Unlimited, 1/5/10/25/50 MB/s, Custom…).
* Active & queued list (one row widget per job): cover thumb, title + option
  label, state label, progress bar (`state` property for colour), detail line
  ("1.2 GB of 4.5 GB · 12.3 MB/s · 4m left", "Extracting 45%", "Waiting 42s",
  error text), buttons: Pause/Resume, Cancel, Move up/down, Retry, Open in
  browser (error_url), Import archive… (after external host), Install
  (downloaded but not installed), Play / Show in library (completed), Remove.
* History section (completed/failed/cancelled), collapsible. Empty state.

Settings page
* Section list left, scrollable form right; changes apply immediately via
  `ctx.settings.update(...)` (no Apply button).
* General: close behaviour, start minimized, launch with Windows
  (`system.autostart`), notifications, app update checks.
* Library: library folders (add/remove/default), shortcut toggles, minimize on
  game launch, show hidden games, game update checks + interval.
* Downloads: download folder, concurrent downloads, connections per download,
  speed limit, auto install, delete archive after install, verify archive,
  auto resume, verification timeout, 7-Zip path (detected path + version +
  Browse/Detect).
* Account: signed-in user / Sign in / Sign out, remember sign-in, open account
  page on website.
* Appearance: theme cards (swatches; click applies live).
* Advanced: catalog stats + Sync now (progress), image cache size + Clear,
  log level, Open logs folder, Open data folder, Reset settings, Run setup
  wizard again.
* About: logo, version, links (GitHub, releases, report an issue), Check for
  updates, disclaimer, third-party notices.

Dialogs
* Login: email, password (show/hide), remember me, Sign in; "Continue with
  Discord" (embedded browser → `/auth/discord` → cookies →
  `auth.login_with_cookies`); links to register / forgot password on the
  website; inline errors; busy state.
* Verification: see `ui/dialogs/verification.py` and "Site protocol" above.
* First-run wizard: Welcome (unofficial-client disclaimer) → Library folder →
  7-Zip → Preferences (theme, shortcuts, close behaviour) → Account (optional)
  → Finish (`first_run_completed=True`).

## Testing

* `tests/unit/` — pure logic: parsers against the saved live pages in
  `tests/fixtures/site/`, engine against a local Range-capable HTTP server,
  manager/installer/library with temp dirs and fakes.
* `tests/gui/` — pytest-qt, offscreen, using `tests/fakes.FakeContext`.
* `tests/live/` — `@pytest.mark.live`, opt-in (`ANKER_LIVE_TESTS=1`).
* `python -m tests.fakes` opens the real UI on fake data for manual review.
