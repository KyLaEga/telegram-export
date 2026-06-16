# Telegram Export

A native **PySide6 desktop app** (with a battle-tested CLI engine underneath) for
**downloading all media from a Telegram channel** to a local SSD and **re-uploading**
a folder back to a channel — with deduplication, best-quality resolution, resumable
state, and a multi-worker download dashboard.

> 🇷🇺 Русская версия README — [README.ru.md](README.ru.md). The app ships in **English
> and Russian**, switchable live from the header.

---

## Features

- **Native GUI** — a 6-screen wizard: Login → Settings → Duplicate Review → Download
  Dashboard → Upload Pipeline → Reports.
- **Dark & light themes** — a Discord-style design system, switchable at runtime
  (dark by default).
- **English / Russian** — full localisation, switchable live from the header bar; the
  choice is remembered between sessions.
- **Streaming, zero-in-memory I/O** — network chunks are written straight to the external
  SSD, so a 50 GB file never tries to fit in RAM and the Mac's internal disk is spared.
- **Smart downloading**
  - **Deduplication** — the same file posted multiple times is downloaded once
    (matched by name + size, and duration for video).
  - **Best-quality resolution** — when a video exists in several qualities, only the
    highest resolution/bitrate is kept; worse copies are dropped before they hit disk.
  - **"Download everything" + review** — preview the plan and bring individual skipped
    duplicates/worse versions back before the run starts.
  - **Multi-worker, multi-connection** downloads (FastTelethon) with per-worker progress
    bars and a live operations log.
  - **Resumable & immortal state** — a `download_state.db` that a `--rescan` never
    touches, plus a disk-recovery scanner; the filesystem is the source of truth.
  - **Automatic FloodWait handling** and reconnection after dropped connections.
  - **faststart remux** so macOS spacebar preview and instant seeking work out of the box
    (toggleable).
- **Upload Pipeline** — send a folder back to a channel as native media (video with
  preview) with idempotent "skip already-uploaded" tracking (`upload_state.db`).
- **Reports** — per-run summary cards plus one-click access to the text reports the
  engine leaves in the destination folder. An `--verify` audit mode checks integrity
  without downloading.

---

## Requirements

- **Python 3.12**
- **macOS** (primary target; the engine is cross-platform, the GUI is tuned for macOS)
- A Telegram **API ID** and **API hash** from <https://my.telegram.org>
- See [`requirements.txt`](requirements.txt) — Pyrogram, TgCrypto, PySide6, etc.

---

## Install

```bash
git clone https://github.com/KyLaEga/telegram-export.git
cd telegram-export

python3.12 -m venv venv
venv/bin/python3.12 -m pip install -r requirements.txt
```

---

## Configuration

The app reads its credentials from `config.json` (the same file the CLI uses).
**This file is git-ignored and must never be committed** — it contains your API ID,
API hash, phone number, channel and destination path.

Copy the template and fill it in (or just enter the values on the Login screen, which
writes `config.json` for you):

```bash
cp config.example.json config.json
```

```jsonc
{
  "api_id": 0,                                   // number from my.telegram.org
  "api_hash": "your_32_char_api_hash",           // string from my.telegram.org
  "phone": "+10000000000",                       // your phone, intl format
  "channel": "@your_channel_or_t.me_link_or_id", // source channel
  "dest": "/Volumes/SSD/telegram_export"         // download destination
}
```

> 🔒 **Security note.** `config.json`, `*.session` (a live login token), `*.db` and
> `*.log` are all listed in [`.gitignore`](.gitignore). Never share your `.session`
> file — it grants access to your Telegram account.

---

## Run the GUI

```bash
venv/bin/python3.12 run_gui.py
```

On first launch, sign in on the **Login** screen (you'll be prompted for the Telegram
code and, if enabled, your 2FA cloud password). Then choose options and start the export.

## Run the CLI engine directly

The GUI is a thin layer over `export_media.py`. The engine still works standalone:

```bash
# Download (uses config.json; see --help for all flags)
venv/bin/python3.12 export_media.py

# Clean re-index without re-downloading
venv/bin/python3.12 export_media.py --rescan

# Audit on-disk files against the plan
venv/bin/python3.12 export_media.py --verify

# Upload a folder back to a channel
venv/bin/python3.12 uploader.py --target @your_channel /path/to/folder
```

Run `export_media.py --help` for the full set of flags (connections, workers, dedup,
quality, faststart, reset, etc.).

---

## Tests

A dependency-free regression suite (no pytest needed):

```bash
QT_QPA_PLATFORM=offscreen venv/bin/python3.12 tests/run_tests.py
```

It covers the Reporter contract, the options/checkbox logic, the Reports screen, the
localisation engine (EN/RU key parity + live switching), and the dark/light theme toggle.

---

## Project layout

```
export_media.py        # download engine (Pyrogram/asyncio) + CLI
uploader.py            # upload pipeline (folder → channel) + upload_state.db
fast_download.py       # FastTelethon multi-connection helper
run_gui.py             # GUI launcher
gui/
  app.py               # MainWindow: header + screen stack + wiring
  theme.py             # TensorMedia design system (dark/light palettes, widgets)
  i18n.py              # translation engine (EN default + RU)
  topbar.py            # header: theme + language switchers
  controller.py        # Qt <-> asyncio bridge (background engine thread)
  reporter.py          # thread-safe event queue -> Qt signals
  screens/             # login, options, review, dashboard, upload, reports
tests/run_tests.py     # regression suite
config.example.json    # template (copy to config.json)
```

---

## License

[MIT](LICENSE) © 2026 KyLaEga
