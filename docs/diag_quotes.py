#!/usr/bin/env python3
"""For each failing quote, locate the closest region in the fetched page and print it."""
import json, re, ssl, urllib.request, html as H

CAT = "<repo>/docs/model-catalog-research-cn.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
ctx = ssl.create_default_context()

def norm(s):
    return re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip().lower()

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        raw = r.read()
    for enc in ("utf-8", "gb18030"):
        try: return raw.decode(enc)
        except UnicodeDecodeError: pass
    return raw.decode("utf-8", errors="ignore")

def strip_html(html):
    html = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
    html = re.sub(r"<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", html)
    txt = re.sub(r"<[^>]+>", " ", html)
    return H.unescape(txt)

data = json.load(open(CAT, encoding="utf-8"))
pages = {}
fails = []
for v in data["vendors"]:
    for m in v["models"]:
        for e in m["evidence"]:
            u = e["url"]
            if u not in pages:
                try: pages[u] = strip_html(fetch(u))
                except Exception: pages[u] = ""
            if norm(e["quote"]) not in norm(pages[u]):
                q2 = re.sub(r"[^\x00-\x7f]", "", norm(e["quote"]))
                p2 = re.sub(r"[^\x00-\x7f]", "", norm(pages[u]))
                if not (q2 and q2 in p2):
                    fails.append((u, m["model"], e["quote"]))

print("failing:", len(fails))
for u, model, q in fails:
    page = pages.get(u, "")
    # take up to 6 key tokens from quote, find densest window
    toks = [t for t in re.split(r"\s+", norm(q)) if len(t) >= 4 and re.match(r"[\w\u4e00-\u9fff]", t)][:6]
    best, bestpos = 0, 0
    n = norm(page)
    for i in range(0, max(1, len(n) - 400), 100):
        win = n[i:i+400]
        score = sum(1 for t in toks if t in win)
        if score > best: best, bestpos = score, i
    print("=" * 70)
    print("URL:", u)
    print("MODEL:", model)
    print("WANTED:", q[:160])
    print("PAGE AROUND BEST MATCH (score %d/%d):" % (best, len(toks)))
    print(page[bestpos*1:bestpos+400] if False else n[bestpos:bestpos+420])
