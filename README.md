# Grenadier Scanner — Auto-Updating Feature Page

A self-running tracker for the Agile Off-Road **INEOS Scanner** forum thread. A GitHub
Action scrapes the thread once a day, asks the Anthropic API to classify any **new** posts
as *Demonstrated / Official / Rumored / Not supported*, regenerates `index.html`, and commits
it. GitHub Pages then serves the updated page automatically.

```
cron (daily) ─▶ scrape every thread page ─▶ classify NEW posts (Claude)
        ─▶ update state/*.json ─▶ render index.html ─▶ git commit ─▶ Pages redeploys
```

## Files

| File | Purpose |
|------|---------|
| `index.template.html` | The styled page with placeholders for the dynamic tables. |
| `index.html` | **Generated** output (served by Pages). Created on first run. |
| `update.py` | Scraper + classifier + renderer. |
| `state/seen.json` | IDs of posts already processed (so each run is incremental). |
| `state/features.json` | The categorized feature dataset (source of truth). |
| `.github/workflows/update.yml` | The daily schedule. |
| `requirements.txt` | Python deps. |

> Put **all of these at the root of your repo** (the same repo serving GitHub Pages), so
> `index.html` lives at the repo root and loads at your bare Pages URL.

## One-time setup

1. **Create the repo & add these files** (root level), commit, push to `main`.
2. **Add your Anthropic key as a secret:** repo **Settings → Secrets and variables →
   Actions → New repository secret**, name it `ANTHROPIC_API_KEY`, paste your key.
   (Get a key at <https://console.anthropic.com>.)
3. **Enable Pages:** **Settings → Pages → Source = Deploy from a branch → `main` / root.**
4. **Run it once now:** **Actions** tab → *Update Grenadier feature page* → **Run workflow**.
   The first run backfills the whole thread (every existing post is classified), so it takes
   a few minutes and uses the most API tokens. Every run after that only touches new posts.

That's it — from then on it updates itself daily.

## Cost & timing

- The schedule is `0 6 * * *` (06:00 UTC daily). Change the `cron` line in
  `update.yml` — see <https://crontab.guru>. `workflow_dispatch` lets you trigger it by hand.
- Model defaults to `claude-haiku-4-5` (cheap). The **first** run classifies the whole
  thread (~hundreds of posts) — typically well under a dollar. Daily runs classify only the
  handful of new posts, usually a fraction of a cent.
- GitHub Actions minutes for this are free on public repos.

## Notes & tuning

- **Exact timestamps:** the scraper reads each post's `<time datetime=...>` attribute, so
  the page now shows real **date *and* time** per post (an improvement over the original).
- **New pages handled automatically:** it walks `page-2`, `page-3`, … until a page has no
  posts, so it keeps working as the thread grows past page 17.
- **The "Competitive gaps" section is static** — it's curated analysis, not scraped, so it
  stays as written in `index.template.html`. Edit it there if you want to change it.
- **Politeness:** one thread, 2-second delay between pages, identifying User-Agent. Respect
  the forum's Terms of Service; if asked to stop, stop. This is a personal, low-volume tracker.
- **Re-categorize everything:** delete `state/seen.json` and `state/features.json` and run
  again to rebuild from scratch (e.g. after you tweak the classification prompt in `update.py`).
- **Change the thread:** set a `THREAD_URL` env var (or edit the default in `update.py`).

## Run locally (optional)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python update.py
open index.html
```
