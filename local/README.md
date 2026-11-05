# Market Agent V4.5 — Laptop Mode (manual runs)

Normally Market Agent runs on GitHub (https://aufisn.github.io/market-agent/). This laptop mode is the fully independent fallback: it needs no GitHub account, token or connection.

Runs the full V4.5 system on this Mac with **no schedule**. Nothing runs, and no Claude credit is spent, unless you press a Run button in the dashboard.

## Layout

| Where | What |
|---|---|
| This work folder | Source of truth for **code**. Edit code here only. |
| `~/market-agent` | The install: venv, `.env`, `.trigger_token`, `watchlist.csv`, `logs/`, `data/` |
| `~/market-agent-v2.5` | Archive of the old June install (untouched), including `old-schedule-backup/` with the removed launchd plist and crontab |

`start.sh` copies the latest code from this folder into `~/market-agent` every time it starts. It never touches secrets or data.

## First time

Already done on this Mac. To set up again, or on another Mac:

```bash
bash "<your work folder>/local/setup_mac.sh"
```

Then put a valid Anthropic key in `.env`:

```bash
open -e ~/market-agent/.env
```

## Every time

```bash
bash ~/market-agent/local/start.sh
```

This opens http://localhost:8000. The first time, the dashboard asks for the access token. Copy it with:

```bash
pbcopy < ~/market-agent/.trigger_token
```

All the dashboard buttons work: Run Agent, Pre Market, Day Insight, Refresh Universe, Record Trade, the Simulator reset and watchlist edits. Run output goes to `~/market-agent/logs/trigger.log`.

Press **Ctrl+C** in that terminal to stop. That also stops a run in progress, so let a run finish first.

## Backup and restore

Backups are plain (not encrypted) dated files, `market-agent-backup-YYYY-MM-DD.tar.gz`, holding `watchlist.csv`, `logs/` and `data/`. Keep them somewhere private. A backup made on GitHub and one made on the laptop are the same format, so either restores in either place.

**Make a backup**
- Any dashboard (GitHub or laptop): Run tab → **💾 Download backup**. It asks where to save if the browser can.
- Laptop data, from Terminal (the folder is optional; default `~/Documents/Market Agent Backups`):

```bash
bash ~/market-agent/local/backup_local.sh ~/Documents/"Market Agent Backups"
```

- GitHub (Gist) data, from Terminal (asks for your passphrase; the Gist ID is read from `~/market-agent/.gist_id`):

```bash
~/market-agent/venv/bin/python ~/market-agent/ci/state_store.py backup --to ~/Documents/"Market Agent Backups"
```

**Restore on the laptop** (the safe-fail option: needs no GitHub at all). It saves the laptop's current data as a `-before-restore` backup first, then asks you to type RESTORE:

```bash
bash ~/market-agent/local/restore_local.sh ~/Documents/"Market Agent Backups"/market-agent-backup-YYYY-MM-DD.tar.gz
```

Then start Market Agent as usual (`bash ~/market-agent/local/start.sh`) and carry on running on the laptop.

**Restore to GitHub** (back to the Gist; saves the Gist's current data first, discards pending phone edits):

```bash
~/market-agent/venv/bin/python ~/market-agent/ci/state_store.py restore ~/Documents/"Market Agent Backups"/market-agent-backup-YYYY-MM-DD.tar.gz
```

Don't run on the laptop and on GitHub in the same period: each keeps its own copy. To switch, back up one side and restore it on the other.

## Suggested first run

1. In the Watchlist tab, check holdings and cash. They were carried over from June.
2. In the Run tab, press **Refresh Universe**. It builds the 300-stock candidate list without using Claude.
3. In the Run tab, press **Run Agent**. This is the full pipeline and uses Claude credit.

## Scheduling later

`scheduler.sh` is kept but nothing starts it. To schedule later, run it under launchd (the Mac must be awake at 10:30 New York time).
