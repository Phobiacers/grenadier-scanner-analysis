#!/usr/bin/env python3
"""
Agile / INEOS Grenadier scanner thread -> auto-updating feature page.

What it does on every run:
  1. Scrapes every page of the forum thread (auto-detecting how many pages exist).
  2. Finds posts it has never seen before (tracked in state/seen.json).
  3. Sends each NEW post to the Anthropic API, which classifies it as
     demonstrated / official / rumored / not_supported / none and pulls a short quote.
  4. Merges results into state/features.json (the source of truth).
  5. Re-renders index.html from index.template.html.

Designed to be run by GitHub Actions on a schedule. Idempotent: re-running with no
new posts produces no changes, so the workflow only commits when something is new.

Env vars:
  ANTHROPIC_API_KEY   (required) – your Anthropic key
  ANTHROPIC_MODEL     (optional) – default 'claude-haiku-4-5'
  THREAD_URL          (optional) – override the thread base URL
"""

import os, re, sys, json, time, html, datetime, pathlib
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config
THREAD_URL = os.environ.get(
    "THREAD_URL",
    "https://www.theineosforum.com/threads/"
    "agile-servicing-software-service-reset-tpms-management-and-more%E2%80%A6.12421872/",
).rstrip("/") + "/"
MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
HERE    = pathlib.Path(__file__).resolve().parent
STATE   = HERE / "state"
STATE.mkdir(exist_ok=True)
SEEN_F  = STATE / "seen.json"
FEAT_F  = STATE / "features.json"
TPL_F   = HERE / "index.template.html"
OUT_F   = HERE / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GrenadierFeatureBot/1.0; "
                  "+personal feature tracker, polite single-thread scrape)"
}
MAX_PAGES   = 200          # hard safety cap
PAGE_DELAY  = 2.0          # seconds between page fetches (be polite)
CATEGORIES  = {"demonstrated", "official", "rumored", "not_supported"}

# ---------------------------------------------------------------- scraping
def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

def page_url(n):
    return THREAD_URL if n == 1 else f"{THREAD_URL}page-{n}"

def parse_posts(page_html, page_no):
    """Return list of dicts for each post article on a XenForo page."""
    soup = BeautifulSoup(page_html, "html.parser")
    posts = []
    for art in soup.select("article.message--post, article.message"):
        # post id (e.g. "post-1333392558")
        cid = art.get("data-content") or ""
        if not cid.startswith("post-"):
            continue
        author = art.get("data-author") or ""
        # exact timestamp from the <time datetime="..."> element
        t = art.find("time")
        iso = (t.get("datetime") if t else "") or ""
        # post number + permalink (#NN link in the attribution bar)
        num, url = "", page_url(page_no) + "#" + cid
        link = art.select_one(".message-attribution-opposite a[href*='/post-'], "
                              "a.message-attribution-gadget[href*='/post-']")
        if not link:
            # fallback: any anchor whose text is like "#123"
            for a in art.select("a"):
                if a.get_text(strip=True).startswith("#"):
                    link = a; break
        if link:
            num = link.get_text(strip=True).lstrip("#")
            href = link.get("href", "")
            if href:
                url = href if href.startswith("http") else \
                      "https://www.theineosforum.com" + href
        # body text (strip nested quotes so we judge the author's own words)
        body = art.select_one(".message-body .bbWrapper") or art.select_one(".bbWrapper")
        text = ""
        if body:
            for bq in body.select("blockquote"):
                bq.decompose()
            text = body.get_text("\n", strip=True)
        posts.append({"id": cid, "author": author, "iso": iso,
                      "num": num, "url": url, "page": page_no, "text": text})
    return posts

def scrape_all():
    all_posts, page = [], 1
    while page <= MAX_PAGES:
        try:
            ph = fetch(page_url(page))
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                break
            raise
        posts = parse_posts(ph, page)
        if not posts:
            break
        all_posts.extend(posts)
        # stop if this page repeats the last id of the previous page (no real next page)
        print(f"  page {page}: {len(posts)} posts")
        page += 1
        time.sleep(PAGE_DELAY)
    # de-dupe by id, keep first occurrence
    seen, uniq = set(), []
    for p in all_posts:
        if p["id"] not in seen:
            seen.add(p["id"]); uniq.append(p)
    return uniq

# ---------------------------------------------------------------- classification
SYSTEM = (
    "You analyze posts from a forum thread about the Agile Offroad 'INEOS Scanner' — "
    "a Windows OBDII diagnostic & coding tool for the Ineos Grenadier. "
    "Decide whether a post describes a SOFTWARE FEATURE or CAPABILITY, and classify it.\n"
    "Categories:\n"
    "  demonstrated  – someone shows it working (screenshots, 'I did X', 'it works').\n"
    "  official      – stated/confirmed/committed by Agile, the developer 'John', or the "
    "liaison 'Itsdchz' relaying them (incl. 'in the works', 'on the roadmap', pricing, "
    "release date, platform/hardware facts).\n"
    "  rumored       – a user request / wishlist / speculation not confirmed by Agile.\n"
    "  not_supported – explicitly stated the tool cannot or will not do it.\n"
    "  none          – general chit-chat, no feature content.\n"
    "A post may contain MORE THAN ONE feature. Return STRICT JSON only, an array; "
    "each element: {\"category\":..., \"feature\":\"short label\", "
    "\"quote\":\"<=1 sentence verbatim from the post\"}. "
    "Use [] if the post has no feature content. No prose outside the JSON."
)

def classify(client, post):
    snippet = post["text"][:6000]
    if not snippet.strip():
        return []
    user = (f"Author: {post['author']}\nPost #{post['num']} (page {post['page']})\n\n"
            f"POST TEXT:\n{snippet}")
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=1024, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            m = re.search(r"\[.*\]", raw, re.S)
            data = json.loads(m.group(0) if m else raw)
            out = []
            for d in data:
                cat = str(d.get("category", "")).strip().lower()
                if cat in CATEGORIES and d.get("feature"):
                    out.append({"category": cat,
                                "feature": str(d["feature"]).strip(),
                                "quote": str(d.get("quote", "")).strip()})
            return out
        except Exception as e:
            if attempt == 2:
                print(f"  ! classify failed for {post['id']}: {e}")
                return []
            time.sleep(2 * (attempt + 1))

# ---------------------------------------------------------------- rendering
def fmt_date(iso):
    """'2026-05-12T18:48:01-0700' -> ('12 May 2026','18:48')."""
    if not iso:
        return ("—", "—")
    try:
        s = iso.replace("Z", "+00:00")
        # XenForo gives +0000 (no colon) sometimes; normalize
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
        dt = datetime.datetime.fromisoformat(s)
        return (dt.strftime("%-d %b %Y"), dt.strftime("%H:%M"))
    except Exception:
        return (iso[:10], iso[11:16] if len(iso) > 15 else "—")

def esc(s):
    return html.escape(s or "", quote=True)

def rows_for(features, category):
    items = [f for f in features if f["category"] == category]
    # sort by timestamp then post number
    items.sort(key=lambda f: (f.get("iso", ""), int(f.get("num") or 0)))
    out = []
    for f in items:
        d, t = fmt_date(f.get("iso", ""))
        when = f"{d}" + (f"<br><span style='color:#9a9da1'>{t}</span>" if t != "—" else "")
        src = f.get("num") or "link"
        out.append(
            "<tr>"
            f"<td class='feat'>{esc(f['feature'])}</td>"
            f"<td class='date'>{when}</td>"
            f"<td>{esc(str(f.get('page','')))}</td>"
            f"<td class='q'>{esc(f.get('quote',''))}</td>"
            f"<td class='src'><a href='{esc(f.get('url','#'))}' target='_blank'>#{esc(src)}</a></td>"
            "</tr>"
        )
    return "\n".join(out) or "<tr><td colspan='5' style='color:#9a9da1'>No items yet.</td></tr>"

def render(features):
    tpl = TPL_F.read_text(encoding="utf-8")
    counts = {c: sum(1 for f in features if f["category"] == c) for c in CATEGORIES}
    repl = {
        "<!--ROWS:demo-->":  rows_for(features, "demonstrated"),
        "<!--ROWS:off-->":   rows_for(features, "official"),
        "<!--ROWS:rumor-->": rows_for(features, "rumored"),
        "<!--ROWS:no-->":    rows_for(features, "not_supported"),
        "{{N_DEMO}}":  str(counts["demonstrated"]),
        "{{N_OFF}}":   str(counts["official"]),
        "{{N_RUMOR}}": str(counts["rumored"]),
        "{{N_NO}}":    str(counts["not_supported"]),
        "{{N_GAP}}":   "25",  # competitive gaps are a static curated section
        "{{LAST_UPDATED}}": datetime.datetime.now(datetime.timezone.utc)
                              .strftime("%-d %b %Y %H:%M UTC"),
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    OUT_F.write_text(tpl, encoding="utf-8")

# ---------------------------------------------------------------- main
def main():
    seen     = set(json.loads(SEEN_F.read_text())) if SEEN_F.exists() else set()
    features = json.loads(FEAT_F.read_text()) if FEAT_F.exists() else []

    print(f"Scraping: {THREAD_URL}")
    posts = scrape_all()
    print(f"Total posts on thread: {len(posts)}  (already seen: {len(seen)})")
    new = [p for p in posts if p["id"] not in seen]
    print(f"New posts to classify: {len(new)}")

    if new:
        import anthropic
        client = anthropic.Anthropic()
        for i, p in enumerate(new, 1):
            print(f"[{i}/{len(new)}] {p['id']} (#{p['num']})")
            for f in classify(client, p):
                f.update(iso=p["iso"], page=p["page"], url=p["url"],
                         author=p["author"], num=p["num"], id=p["id"])
                features.append(f)
            seen.add(p["id"])

    # de-dupe features (same post + same feature label)
    uniq, keys = [], set()
    for f in features:
        k = (f.get("id"), f.get("feature"))
        if k not in keys:
            keys.add(k); uniq.append(f)
    features = uniq

    SEEN_F.write_text(json.dumps(sorted(seen), indent=0))
    FEAT_F.write_text(json.dumps(features, indent=1, ensure_ascii=False))
    render(features)
    print(f"Done. {len(features)} feature entries across "
          f"{len({f['category'] for f in features})} categories. Wrote index.html")

if __name__ == "__main__":
    main()
