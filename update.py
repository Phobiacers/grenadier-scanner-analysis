#!/usr/bin/env python3
"""
Agile / INEOS Grenadier scanner thread -> FROZEN baseline + guarded new-feature tracker.

Philosophy (v2):
  * The curated report baked into index.template.html is FROZEN. The script never
    touches it.
  * The only thing the script edits is the "New Since Baseline" section. A forum post
    is added there ONLY when it is:
       (a) newer than the baseline (its post id is not in state/seen.json), AND
       (b) judged by the model to describe a GENUINELY NEW feature / development that is
           NOT already represented in the curated baseline (state/baseline.json) or in
           items already added.
  * Result: no bloat, no rewriting your good analysis, and the live page only changes
    when something genuinely new appears.

State files:
  state/baseline.json   – the 68 curated feature labels (the "already known" set). Static.
  state/seen.json       – post ids already processed (pre-seeded with the whole thread).
  state/new_features.json – the small, growing list of genuinely-new items. Authored here.

Env: ANTHROPIC_API_KEY (required), ANTHROPIC_MODEL (default claude-haiku-4-5),
     THREAD_URL (optional override).
"""

import os, re, sys, json, time, html, datetime, pathlib
from browserclient import BrowserClient
from bs4 import BeautifulSoup

THREAD_URL = os.environ.get(
    "THREAD_URL",
    "https://www.theineosforum.com/threads/"
    "agile-servicing-software-service-reset-tpms-management-and-more%E2%80%A6.12421872/",
).rstrip("/") + "/"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

HERE  = pathlib.Path(__file__).resolve().parent
STATE = HERE / "state"; STATE.mkdir(exist_ok=True)
SEEN_F = STATE / "seen.json"
BASE_F = STATE / "baseline.json"
NEW_F  = STATE / "new_features.json"
TPL_F  = HERE / "index.template.html"
OUT_F  = HERE / "index.html"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GrenadierFeatureBot/2.0; polite single-thread tracker)"}
MAX_PAGES, PAGE_DELAY = 200, 2.0
CATEGORIES = {"demonstrated", "official", "rumored", "not_supported"}
CAT_LABEL  = {"demonstrated": "Demonstrated", "official": "Official",
              "rumored": "Rumored", "not_supported": "Not supported"}
CAT_PILL   = {"demonstrated": "p-demo", "official": "p-off",
              "rumored": "p-rumor", "not_supported": "p-no"}

# ----------------------------------------------------------------- scraping
def page_url(n):
    return THREAD_URL if n == 1 else f"{THREAD_URL}page-{n}"

def parse_posts(page_html, page_no):
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for art in soup.select("article.message--post, article.message"):
        cid = art.get("data-content") or ""
        if not cid.startswith("post-"):
            continue
        author = art.get("data-author") or ""
        t = art.find("time"); iso = (t.get("datetime") if t else "") or ""
        # decouple permalink (href with /post-) from the visible "#NN" number
        perma, num = "", ""
        for a in art.select("a[href]"):
            href = a.get("href", "")
            if "/post-" in href and not perma:
                perma = href if href.startswith("http") else "https://www.theineosforum.com" + href
            txt = a.get_text(strip=True)
            if re.fullmatch(r"#\d+", txt) and not num:
                num = txt[1:]
            if perma and num:
                break
        url = perma or (page_url(page_no) + "#" + cid)
        body = art.select_one(".message-body .bbWrapper") or art.select_one(".bbWrapper")
        text = ""
        if body:
            for bq in body.select("blockquote"):
                bq.decompose()
            text = body.get_text("\n", strip=True)
        out.append({"id": cid, "author": author, "iso": iso, "num": num,
                    "url": url, "page": page_no, "text": text})
    return out

def scrape_all():
    posts, page = [], 1
    scraped_ids = set()
    with BrowserClient() as client:
        while page <= MAX_PAGES:
            url = page_url(page)
            resp = client.get(url)
            if resp is None:
                raise RuntimeError(f"Failed to fetch {url}")
            if resp.status_code == 404:
                break
            got = parse_posts(resp.text, page)
            if not got:
                break
            
            # Check for duplicate posts in the current run (detects XenForo redirect to last page)
            has_new = False
            for p in got:
                if p["id"] not in scraped_ids:
                    scraped_ids.add(p["id"])
                    has_new = True
            
            if not has_new:
                print(f"  page {page} returned duplicate posts; ending scrape.")
                break

            posts.extend(got); print(f"  page {page}: {len(got)} posts"); page += 1
    seen, uniq = set(), []
    for p in posts:
        if p["id"] not in seen:
            seen.add(p["id"]); uniq.append(p)
    return uniq

# ----------------------------------------------------------------- novelty gate
_STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "with", "set", "new",
         "enable", "disable", "change", "via", "your", "all"}

def _tokens(s):
    raw = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
    toks = set()
    for w in raw:
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s"):   # crude singularize (thresholds->threshold)
            w = w[:-1]
        toks.add(w)
    return toks

def _norm(s):
    return " ".join(sorted(_tokens(s)))

def _similar(a, b):
    """True if two feature labels likely describe the same thing."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    na, nb = " ".join(sorted(ta)), " ".join(sorted(tb))
    if na in nb or nb in na:
        return True
    inter = len(ta & tb)
    return inter / max(1, min(len(ta), len(tb))) >= 0.6

def is_duplicate(feature, known_labels):
    return any(_similar(feature, k) for k in known_labels)

SYSTEM = (
    "You triage posts from a forum thread about the Agile Offroad 'INEOS Scanner' "
    "(a Windows OBDII diagnostic & coding tool for the Ineos Grenadier). "
    "Your job is HIGH-PRECISION: only surface a post when it announces or demonstrates a "
    "GENUINELY NEW feature, capability, or concrete development.\n"
    "You are given a list of ALREADY-KNOWN features. If the post merely discusses, asks "
    "about, repeats, reacts to, or is a small variation of something already known — or is "
    "general chit-chat, opinion, pricing debate, or thanks — return an EMPTY array.\n"
    "Categories: demonstrated (shown working), official (confirmed/committed by Agile, dev "
    "'John', or liaison 'Itsdchz', incl. 'in the works'/roadmap/firm dates), rumored (a "
    "clearly new user feature request not previously raised), not_supported (newly stated "
    "the tool can't/won't do something).\n"
    "Return STRICT JSON only: an array (usually empty or ONE item). Each item: "
    '{"category":..., "feature":"short label", "quote":"<=1 sentence verbatim"}. '
    "No prose outside the JSON. Be conservative: when in doubt, return []."
)

def classify_new(client, post, known_labels):
    snippet = (post["text"] or "").strip()
    if len(snippet) < 8:
        return []
    known_block = "\n".join(f"- {k}" for k in known_labels)
    user = (f"ALREADY-KNOWN FEATURES (do NOT resurface these or close variants):\n{known_block}\n\n"
            f"---\nNEW POST  (author {post['author']}, #{post['num']}, page {post['page']}):\n{snippet[:6000]}")
    for attempt in range(3):
        try:
            msg = client.messages.create(model=MODEL, max_tokens=700, system=SYSTEM,
                                         messages=[{"role": "user", "content": user}])
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            m = re.search(r"\[.*\]", raw, re.S)
            data = json.loads(m.group(0) if m else raw)
            res = []
            for d in data:
                cat = str(d.get("category", "")).strip().lower()
                feat = str(d.get("feature", "")).strip()
                if cat in CATEGORIES and feat and not is_duplicate(feat, known_labels):
                    res.append({"category": cat, "feature": feat,
                                "quote": str(d.get("quote", "")).strip()})
            return res
        except Exception as e:
            if attempt == 2:
                print(f"  ! classify failed for {post['id']}: {e}")
                return []
            time.sleep(2 * (attempt + 1))

# ----------------------------------------------------------------- render
def fmt_date(iso):
    if not iso:
        return ("—", "")
    try:
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso.replace("Z", "+00:00"))
        dt = datetime.datetime.fromisoformat(s)
        return (dt.strftime("%-d %b %Y"), dt.strftime("%H:%M"))
    except Exception:
        return (iso[:10], iso[11:16] if len(iso) > 15 else "")

def esc(s):
    return html.escape(s or "", quote=True)

def render(new_items):
    tpl = TPL_F.read_text(encoding="utf-8")
    if not new_items:
        rows = ("<tr><td colspan='6' class='empty'>No new features detected since the "
                "8 Jun 2026 baseline. The daily job is watching for them.</td></tr>")
        last = "— none yet (baseline only)"
    else:
        items = sorted(new_items, key=lambda f: (f.get("iso", ""), int(f.get("num") or 0)))
        out = []
        for f in items:
            d, t = fmt_date(f.get("iso", ""))
            when = d + (f"<br><span style='color:#9a9da1'>{t}</span>" if t else "")
            num = f.get("num") or ""
            src = (f"#{esc(num)}" if num else "↗ post")
            pill = CAT_PILL.get(f["category"], "p-na")
            out.append(
                "<tr>"
                f"<td><span class='pill {pill}'>{esc(CAT_LABEL.get(f['category'],''))}</span></td>"
                f"<td class='feat'>{esc(f['feature'])}</td>"
                f"<td class='date'>{when}</td>"
                f"<td>{esc(str(f.get('page','')))}</td>"
                f"<td class='q'>{esc(f.get('quote',''))}</td>"
                f"<td class='src'><a href='{esc(f.get('url','#'))}' target='_blank'>{src}</a></td>"
                "</tr>"
            )
        rows = "\n".join(out)
        newest = max(items, key=lambda f: f.get("iso", ""))
        nd, _ = fmt_date(newest.get("iso", ""))
        last = f"{nd} ({len(items)} item{'s' if len(items)!=1 else ''} added)"
    tpl = tpl.replace("<!--ROWS:new-->", rows).replace("{{LAST_NEW}}", last)
    OUT_F.write_text(tpl, encoding="utf-8")

# ----------------------------------------------------------------- main
def main():
    seen  = set(json.loads(SEEN_F.read_text())) if SEEN_F.exists() else set()
    known = json.loads(BASE_F.read_text()) if BASE_F.exists() else []
    known_labels = [k["feature"] for k in known]
    new_items = json.loads(NEW_F.read_text()) if NEW_F.exists() else []

    print(f"Baseline known features: {len(known_labels)} | already-seen posts: {len(seen)}")
    posts = scrape_all()
    candidates = [p for p in posts if p["id"] not in seen]
    print(f"Thread posts: {len(posts)} | new (post-baseline) candidates: {len(candidates)}")

    added = 0
    if candidates:
        import anthropic
        client = anthropic.Anthropic()
        # running set of labels = baseline + already-added new items (avoid dupes across runs)
        live_labels = list(known_labels) + [n["feature"] for n in new_items]
        for i, p in enumerate(candidates, 1):
            hits = classify_new(client, p, live_labels)
            tag = f" -> +{len(hits)} NEW" if hits else ""
            print(f"[{i}/{len(candidates)}] {p['id']} (#{p['num']}){tag}")
            for f in hits:
                f.update(iso=p["iso"], page=p["page"], url=p["url"],
                         author=p["author"], num=p["num"], id=p["id"])
                new_items.append(f); live_labels.append(f["feature"]); added += 1
            seen.add(p["id"])

    SEEN_F.write_text(json.dumps(sorted(seen), indent=0))
    NEW_F.write_text(json.dumps(new_items, indent=1, ensure_ascii=False))
    render(new_items)
    print(f"Done. Added {added} genuinely-new item(s); {len(new_items)} total in 'New Since Baseline'.")

if __name__ == "__main__":
    main()
