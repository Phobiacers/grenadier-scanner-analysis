# Grenadier Scanner — Frozen Report + Guarded New-Feature Tracker

A self-hosted page that pairs a **curated, frozen analysis** of the Agile Off-Road
**INEOS Scanner** forum thread with a daily job that adds **only genuinely new** features
as the thread grows.

## How it behaves (v2)

- **The curated report is frozen.** The tables baked into `index.template.html` (the
  8 Jun 2026 analysis) never change. The script does not rewrite them.
- **Only new, novel posts get added.** Each day the job scrapes the thread and, for any
  post newer than the baseline, asks the model a high-precision question: *does this post
  describe a genuinely NEW feature/development not already covered?* If yes, it's added to
  the **"New Since Baseline"** section. If it's chit-chat, a repeat, or a variation of a
  known feature, it's dropped.
- **The page only changes when something genuinely new appears** — so it's stable to share
  and won't churn under you.

```
cron (daily) ─▶ scrape every page ─▶ keep only posts newer than baseline
        ─▶ model novelty gate (vs state/baseline.json) ─▶ de-dupe
        ─▶ append survivors to "New Since Baseline" ─▶ commit only if changed
```

## Files

| File | Purpose |
|------|---------|
| `index.template.html` | Frozen curated report + a placeholder for the new-items section. |
| `index.html` | **Generated** page served by Pages. |
| `update.py` | Scraper + novelty gate + renderer. |
| `state/baseline.json` | The 68 curated feature labels = the "already known" set. |
| `state/seen.json` | Post ids already processed (pre-seeded with the whole thread). |
| `state/new_features.json` | The small, growing list of genuinely-new items. |
| `.github/workflows/update.yml` | Daily schedule. |

## Tuning

- **Stricter / looser gate:** edit the `SYSTEM` prompt in `update.py`. It's deliberately
  conservative ("when in doubt, return []"). Loosen that line to catch more; tighten to
  catch less.
- **De-dupe sensitivity:** `_similar()` in `update.py` (threshold 0.6). Raise it to allow
  more near-duplicates through; lower it to merge more aggressively.
- **Schedule:** the `cron` line in `update.yml` (default daily 06:00 UTC). See crontab.guru.
- **Re-baseline:** to fold the current "new" items into the frozen report, move them into
  `index.template.html` by hand and clear `state/new_features.json`.
- **Reset the new section:** delete `state/new_features.json` (the gate re-derives it from
  posts not in `seen.json`).

## First-run note

`state/seen.json` already contains the entire thread, so the next run classifies only posts
beyond the baseline (often zero) — fast and nearly free. Only `claude-haiku-4-5` calls for
brand-new posts cost anything.

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python update.py && open index.html
```
