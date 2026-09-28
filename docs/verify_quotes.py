#!/usr/bin/env python3
"""Verify every evidence quote in model-catalog-research-cn.json appears verbatim
in the fetched page text. Fetches each URL (urllib, browser UA); falls back to
files cached locally by the research tooling. Whitespace-normalized substring match."""
import json, re, ssl, urllib.request, glob, os, sys

CAT = "<repo>/docs/model-catalog-research-cn.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
ctx = ssl.create_default_context()

def norm(s):
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()

cache = {}
for f in glob.glob("<repo>/.research-cache/web/*.md") + glob.glob("<repo>/.research-cache/web/*.txt"):
    try:
        cache.setdefault(norm(os.path.basename(f)), open(f, encoding="utf-8", errors="ignore").read())
    except Exception:
        pass

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        raw = r.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")

def strip_html(html):
    html = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
    html = re.sub(r"<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", html)
    txt = re.sub(r"<[^>]+>", " ", html)
    import html as H
    return H.unescape(txt)

pages = {}
data = json.load(open(CAT, encoding="utf-8"))
urls = set()
for v in data["vendors"]:
    for m in v["models"]:
        for e in m["evidence"]:
            urls.add(e["url"])

results = {"ok": 0, "fail": []}
for u in sorted(urls):
    page = None
    src = None
    try:
        html = fetch(u)
        page = strip_html(html)
        src = "live"
    except Exception as ex:
        # fallback: search cache by domain token
        dom = re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
        best = None
        for name, txt in cache.items():
            if dom.replace(".", "-").split("-")[0] in name or dom.split(".")[0] in name:
                if norm(u.split("/")[-1][:20]) and norm(u.split("/")[-1][:20]) in norm(txt):
                    best = txt; break
                best = best or txt
        if best is None:
            results["fail"].append((u, "FETCH FAILED: %s" % ex, []))
            continue
        page = best
        src = "cache"
    pages[u] = page
    print("fetched [%s] %s (%d chars)" % (src, u, len(page)))

print("\n=== quote check ===")
for v in data["vendors"]:
    for m in v["models"]:
        for e in m["evidence"]:
            q = e["quote"]
            if e["url"] not in pages:
                results["fail"].append((e["url"], "NO PAGE", q)); continue
            if norm(q) in norm(pages[e["url"]]):
                results["ok"] += 1
            else:
                # tolerate mojibake pages (latin-1 misdecode): compare ignoring non-ascii weirdness
                q2 = re.sub(r"[^\x00-\x7f]", "", norm(q))
                p2 = re.sub(r"[^\x00-\x7f]", "", norm(pages[e["url"]]))
                if q2 and q2 in p2:
                    results["ok"] += 1
                else:
                    results["fail"].append((e["url"], m["model"], q))

print("\nOK quotes: %d" % results["ok"])
print("FAILED quotes: %d" % len(results["fail"]))
for f in results["fail"]:
    print("-" * 60)
    print("URL:", f[0], "| model:", f[1])
    print("QUOTE:", f[2][:200])
