# Changelog

All notable changes to AnkerClient are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-10-07

A ground-up rewrite. Settings, remembered sign-in and games installed by 0.x
are picked up automatically.

### Added
- **Store**: Discover rows (trending, upcoming, latest, collections, top games),
  full catalog browsing with server-side sorting (recently added, most viewed,
  most liked, top rated, release date, A–Z), genre and VR filters, infinite
  scroll, instant search over a local catalog index merged with site search,
  and a wishlist.
- **Game pages**: artwork, screenshot carousel with lightbox, description,
  system requirements, version and update date, every download option
  (full game, update-only patches, language packs, launchers).
- **Download manager**: persistent queue that survives restarts, multi-connection
  downloads with resume after pause or crash, global speed limit, concurrent
  download limit, automatic retry with backoff, rate-limit handling, and
  disk-space checks before downloading.
- **Browser verification**: downloads protected by the site's Cloudflare
  check open the site's own page in an embedded browser, where the check runs as
  it would in Chrome; the client then takes over the file transfer. The window
  is a compact card showing only the check itself ("Show full page" reveals
  the rest). When that isn't possible: "Open in browser" and "Import archive".
- **Installer**: 7-Zip extraction with progress (built-in fallback for .zip),
  staging on the target drive, crash-safe replace with rollback, patch and add-on
  overlays, executable detection, Desktop/Start-menu shortcuts, prerequisite
  (redistributable) detection.
- **Library**: grid and list views, filters, sorting, favourites, hidden games,
  playtime and last-played tracking, running-game detection, stop game, launch
  options (arguments, run as administrator), properties, repair, uninstall,
  import existing folders or local archives, multiple library folders.
- **Updates**: detects new versions of installed games (with small patches when
  available) and new AnkerClient releases.
- **Accounts**: email/password and Discord sign-in, remembered securely
  (Windows Credential Manager + DPAPI-encrypted session).
- **App**: first-run setup, system tray with download progress and recent games,
  notifications, single instance, start with Windows, six themes (Midnight,
  Daylight, Classic Steam, Terminal, Vaporwave, Bliss XP), crash reporting to a
  rotating log with secrets scrubbed.
- **Engineering**: layered architecture (core / site / services / ui), SQLite
  storage, CI on Python 3.12–3.13, PyInstaller one-folder build, Inno Setup
  installer, portable zip.

### Fixed
- Downloads saved the site's verification web page instead of the game because
  the site now requires a browser check before handing out files.
- Search no longer crawls up to 80 listing pages per query.

## [0.1.2]
- Last release of the original client.
