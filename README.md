# Telegram Downloader

Desktop and console tool to bulk-download media from a Telegram channel or group you can access with your own account, with resume support, scan caching, benchmark tuning, and persistent history.

The core application is cross-platform Python. Windows launchers are included for convenience, and the same GUI / CLI can also run on Linux or macOS with Python directly.

## Features

- Flat download layout: every file is saved into one destination folder
- Resume support through a local SQLite state database
- Graceful stop: finish active downloads, then stop without starting new files
- Connect and scan before downloading
- Persistent Completed and Errors history across sessions
- Timed benchmark mode to compare several `workers` / `part_size_kb` combinations
- One live progress bar per active download
- Optional ZIP extraction
- `cryptg` support for faster Telethon decryption when available

## Project structure

```text
telegram_downloader/
  config.py      # configuration loading/saving and defaults
  engine.py      # Telegram scan, download, benchmark, progress events
  gui.py         # Tkinter desktop application
  state.py       # SQLite resume/history store
  cli.py         # console entry point

telegram_downloader_gui.py        # thin GUI launcher
telegram_channel_downloader.py    # thin console launcher
launch_gui.bat                   # Windows GUI launcher
launch_console.bat               # Windows console launcher
```

## Requirements

- Python 3.11+ recommended
- Tkinter available in your Python installation
- A Telegram account with access to the target channel
- `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org)
- Windows, Linux, or macOS

## Install and run

1. Create your Telegram app at [my.telegram.org](https://my.telegram.org).
2. Copy `config.ini.example` to `config.ini`.
3. Fill in:

```ini
[telegram]
api_id = 123456
api_hash = 0123456789abcdef0123456789abcdef
source = -1001234567890
```

4. Launch the GUI on Windows:

```powershell
launch_gui.bat
```

Or launch the console version on Windows:

```powershell
launch_console.bat
```

On Linux or macOS, you can launch the same app directly with Python:

```bash
python telegram_downloader_gui.py
```

Or the console version:

```bash
python telegram_channel_downloader.py --config config.ini
```

## First login

If your Telegram session does not exist yet, run the console entry point once. Telethon will ask for:

- your phone number
- the login code
- your 2FA password if enabled

After that, the session is stored locally and the GUI can reuse it.

## Channel source formats

The `source` field accepts:

- a numeric peer id such as `-1001234567890`
- a visible `@username`
- a Telegram Web URL containing the `-100...` id

The most reliable format is the numeric `-100...` id.

## GUI workflow

1. Save your settings
2. Click `Connect & Scan`
3. Review pending files, history, and channel details
4. Click `Start download`
5. Use `Stop gracefully` if you want to finish only the currently active files
6. Use `Settings...` for benchmark tuning, retries, workers, block size, and ZIP behavior

The right side of the GUI shows:

- live global progress
- remaining files and bytes
- current speed
- one progress bar per active download
- Pending, Completed, Errors, and Logs tabs

## Filters

The UI uses category presets plus optional extension filters.

Examples:

- `Music packs`: archives + audio
- `Archives only`
- `Lossless only`
- `Custom` with extra extensions such as `.zip .rar .mp3 .flac`

## Benchmark mode

Benchmark mode downloads a few sample files for short timed windows, tries several worker/block combinations, then keeps the fastest one in the config.

Open it from the `Settings...` window, inside the `Benchmark tuning` section.

Default benchmark behavior:

- short timed runs instead of waiting for full files
- multiple predefined profiles
- automatic application of the best `workers` + `part_size_kb`

## Resume and local state

Local state is stored in `.state/`:

- `.state/telegram_downloader.session`: Telegram login session
- `.state/download_state.sqlite3`: completed/skipped/failed download history

Already completed files are detected and shown in the Completed tab on later runs.

The default destination folder is platform-aware:

- Windows: `~/Downloads/Telegram Files`
- Linux and macOS: `~/Downloads/telegram-files`

## Security and GitHub hygiene

Do not commit these files:

- `config.ini`
- `.state/`
- `.venv/`

This repository includes a `.gitignore` that excludes them by default.

`config.ini.example` is safe to publish because it contains placeholders only.

Before your first push, double-check that `config.ini` is still ignored and that no `.state/` files were added manually.

## Optional ZIP extraction

If `extract_zips = true`, ZIP archives are extracted into the same destination folder.

If `keep_archives = false`, the original ZIP is removed after a successful extraction.

## Notes

- Telegram Premium does not guarantee that API downloads will saturate your line speed.
- In practice, the best gains usually come from `cryptg`, tuned worker count, tuned block size, and avoiding stalled transfers.
- If a transfer freezes, the downloader detects the stall, retries it, and records final failures in the Errors tab.
