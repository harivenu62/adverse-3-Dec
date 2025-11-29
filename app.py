# app.py
"""
SAM-Radar — Smart Adverse Media Radar (with Alias Overrides + Domain Priority)
Drop-in production-ready Streamlit app. Add NEWSDATA_KEY as env var for best coverage.
"""

import os
import time
import logging
import sys
import urllib.parse
import re
import difflib
import json
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import feedparser
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

# ---------- CONFIG ----------
DATA_DIR = Path(".")
ALIAS_OVERRIDES_FILE = DATA_DIR / "alias_overrides.json"
DOMAIN_PRIORITY_FILE = DATA_DIR / "domain_priority.json"
ADMIN_PASSWORD = "admin123"  # optional simple protection for editing (change or remove)
# ----------------------------

# Logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sam-radar")

HEADERS = {"User-Agent": "sam-radar-bot/1.0 (+https://example.com)"}

# Streamlit page
st.set_page_config(page_title="SAM-Radar — Smart Adverse Media Radar", layout="wide")
st.title("🛰️ SAM-Radar — Smart Adverse Media Radar")
st.caption("Sanctions + adverse media scanning (OpenSanctions, NewsData, DuckDuckGo, Bing).")

# ---------- Utility: read/write small json files for overrides ----------
def load_json_file(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception(f"Failed reading {path}")
    return default

def save_json_file(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        logger.exception(f"Failed writing {path}")
        return False

# Initialize override files if not present
if not ALIAS_OVERRIDES_FILE.exists():
    save_json_file(ALIAS_OVERRIDES_FILE, {})
if not DOMAIN_PRIORITY_FILE.exists():
    save_json_file(DOMAIN_PRIORITY_FILE, [])

alias_overrides = load_json_file(ALIAS_OVERRIDES_FILE, {})
domain_priority = load_json_file(DOMAIN_PRIORITY_FILE, [])

# ---------- Sidebar Admin UI ----------
st.sidebar.header("Admin / Settings")
show_admin = st.sidebar.checkbox("Show admin controls", value=False)
if show_admin:
    pwd = st.sidebar.text_input("Admin password (optional)", type="password")
    if ADMIN_PASSWORD and pwd != ADMIN_PASSWORD:
        st.sidebar.warning("Enter correct admin password to edit settings (or change ADMIN_PASSWORD in code).")
        admin_mode = False
    else:
        admin_mode = True

    st.sidebar.subheader("Alias Overrides")
    st.sidebar.markdown("Add manual aliases for an entity. Format: `entity_name: alias1, alias2` (one-per-line)")
    overrides_input = st.sidebar.text_area("Overrides (one mapping per line)", 
                                           value="\n".join([f"{k}: {', '.join(v)}" for k, v in alias_overrides.items()]),
                                           height=140)
    if admin_mode and st.sidebar.button("Save alias overrides"):
        # parse input
        new_map = {}
        for line in overrides_input.splitlines():
            if not line.strip():
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                name = k.strip()
                aliases = [a.strip() for a in v.split(",") if a.strip()]
                new_map[name] = aliases
        alias_overrides = new_map
        save_json_file(ALIAS_OVERRIDES_FILE, alias_overrides)
        st.sidebar.success("Alias overrides saved.")

    st.sidebar.subheader("Domain Priority / Allowlist")
    st.sidebar.markdown("Comma-separated domains to prioritize (e.g., `reuters.com, ft.com, theguardian.com`).")
    domains_input = st.sidebar.text_input("Priority domains", value=", ".join(domain_priority))
    if admin_mode and st.sidebar.button("Save domains"):
        domain_priority = [d.strip().lower() for d in domains_input.split(",") if d.strip()]
        save_json_file(DOMAIN_PRIORITY_FILE, domain_priority)
        st.sidebar.success("Domain priority saved.")

    st.sidebar.markdown("---")
    st.sidebar.caption("Overrides are saved to alias_overrides.json and domain_priority.json in the app root.\nNote: file persistence depends on your host; redeploy may reset files.")

# ---------- Main UI inputs ----------
entity = st.text_input("Company or Individual name", placeholder="e.g. Lukoil, Vijay Mallya, Litasco")
per_source_limit = st.slider("Max results per source (per query)", 1, 20, 6)
use_newsdata_checkbox = st.checkbox("Use NewsData.io fallback (set NEWSDATA_KEY in env)", value=True)
advanced_aliasing = st.checkbox("Use Wikipedia alias expansion (may slow queries)", value=True)
prioritize_domains_ui = st.checkbox("Apply domain priority to results", value=True)

# ---------- Helper functions (summaries, scoring) ----------
def summarize(text, max_chars=400):
    if not text:
        return ""
    return (text[:max_chars] + "...") if len(text) > max_chars else text

def risk_level_from_text(text):
    t = (text or "").lower()
    high = ["fraud","money laundering","scam","crime","corruption","sanction","arrest","convicted","charged"]
    medium = ["investigation","probe","regulatory","lawsuit","review"]
    score = 0
    for w in high:
        if w in t:
            score += 2
    for w in medium:
        if w in t:
            score += 1
    if score >= 2:
        return "High"
    if score == 1:
        return "Medium"
    return "Low"

# ---------- Wikipedia alias fetch (best-effort) ----------
def get_wikipedia_aliases(name, max_aliases=6):
    aliases = {name.strip()}
    try:
        q = urllib.parse.quote(name)
        search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json"
        r = requests.get(search_url, headers=HEADERS, timeout=8)
        if r.ok:
            data = r.json()
            hits = data.get("query", {}).get("search", [])[:4]
            for h in hits:
                title = h.get("title")
                if title and title not in aliases:
                    aliases.add(title)
                    try:
                        page = requests.get(f"https://en.wikipedia.org/w/index.php?title={urllib.parse.quote(title)}&printable=yes", headers=HEADERS, timeout=8).text
                        for m in re.findall(r'\((?:also known as|aka|formerly|previously|also called)[^)]{1,200}\)', page, flags=re.I):
                            cleaned = re.sub(r'^\(|\)$', '', m).strip()
                            cleaned = re.sub(r'^(also known as|aka|formerly|previously|also called)\s*[:\-\s]*', '', cleaned, flags=re.I).strip()
                            if cleaned:
                                aliases.add(cleaned)
                    except Exception:
                        continue
    except Exception:
        logger.exception("Wikipedia alias fetching failed")
    aliases_list = list(aliases)
    return aliases_list[:max_aliases]

# ---------- OpenSanctions check ----------
def check_opensanctions(name):
    results = []
    if not name:
        return results
    try:
        url = f"https://api.opensanctions.org/search?q={urllib.parse.quote(name)}"
        r = requests.get(url, headers=HEADERS, timeout=12)
        logger.info(f"OpenSanctions status: {r.status_code} for {name}")
        if r.ok:
            data = r.json()
            hits = data.get("results") or data.get("data") or []
            for h in hits[:10]:
                results.append({
                    "name": h.get("name") or h.get("label") or "",
                    "type": h.get("schema") or h.get("type") or "",
                    "source": h.get("sources") or h.get("source") or "",
                    "note": h.get("notes") or h.get("summary") or "",
                    "raw": h
                })
    except Exception:
        logger.exception("OpenSanctions query failed")
    return results

# ---------- DuckDuckGo instant ----------
def ddg_instant_search(query, max_results=8):
    out = []
    try:
        params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
        r = requests.get("https://api.duckduckgo.com/", params=params, headers=HEADERS, timeout=12)
        logger.info(f"DDG status: {r.status_code} for {query}")
        if r.ok:
            data = r.json()
            if data.get("AbstractText"):
                out.append({"source": "DuckDuckGo", "title": data.get("Heading", query), "summary": data.get("AbstractText"), "link": data.get("AbstractURL", "")})
            for it in (data.get("Results") or [])[:max_results]:
                out.append({"source": "DuckDuckGo", "title": it.get("Text"), "summary": "", "link": it.get("FirstURL")})
            for topic in (data.get("RelatedTopics") or [])[:max_results]:
                if isinstance(topic, dict):
                    text = topic.get("Text") or topic.get("Name")
                    url = topic.get("FirstURL")
                    if text and url:
                        out.append({"source": "DuckDuckGo", "title": text, "summary": "", "link": url})
    except Exception:
        logger.exception("DuckDuckGo instant search failed")
    seen = set()
    dedup = []
    for o in out:
        key = (o.get("link") or "") + "|" + (o.get("title") or "")
        if key not in seen and o.get("link"):
            dedup.append(o)
            seen.add(key)
        if len(dedup) >= max_results:
            break
    return dedup

# ---------- NewsData fallback ----------
def newsdata_fetch(query, max_results=6):
    out = []
    key = os.environ.get("NEWSDATA_KEY", "").strip()
    if not key:
        return out
    try:
        url = f"https://newsdata.io/api/1/news?apikey={key}&q={urllib.parse.quote(query)}&language=en"
        r = requests.get(url, headers=HEADERS, timeout=12)
        logger.info(f"NewsData status: {r.status_code} for {query}")
        if r.ok:
            data = r.json()
            for a in data.get("results", [])[:max_results]:
                out.append({"source": "NewsData", "title": a.get("title"), "summary": a.get("description") or a.get("content",""), "link": a.get("link")})
    except Exception:
        logger.exception("NewsData fetch failed")
    return out

# ---------- Bing lightweight scraping ----------
def bing_search(query, max_results=6):
    out = []
    try:
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=HEADERS, timeout=12)
        logger.info(f"Bing status: {r.status_code} for {query}")
        if r.ok:
            soup = BeautifulSoup(r.text, "lxml")
            blocks = soup.select("li.b_algo")[:max_results]
            for b in blocks:
                h2 = b.find("h2")
                title = h2.get_text(strip=True) if h2 else ""
                link_tag = h2.find("a") if h2 else None
                link = link_tag["href"] if link_tag and link_tag.has_attr("href") else ""
                snippet = (b.find("p").get_text(strip=True) if b.find("p") else "")
                if link:
                    out.append({"source": "Bing", "title": title, "summary": snippet, "link": link})
    except Exception:
        logger.exception("Bing search failed")
    return out

# ---------- Relevance helpers ----------
def is_relevant_to_aliases(aliases, title, summary):
    text = ((title or "") + " " + (summary or "")).lower()
    for a in aliases:
        if re.search(r'\b' + re.escape(a.lower()) + r'\b', text):
            return True
    for a in aliases:
        ratio = difflib.SequenceMatcher(None, a.lower(), (title or "").lower()).ratio()
        if ratio >= 0.78:
            return True
    adverse = ["fraud","sanction","arrest","investigation","scam","money laundering","charged","lawsuit","fine","convicted","corruption"]
    if any(k in text for k in adverse):
        return True
    return False

# ---------- Build query variations ----------
def build_query_variations(alias):
    alias = alias.strip()
    suffixes = ["", " plc", " ltd", " llc", " inc", " corp", " group", " holdings"]
    keywords = ["fraud","scam","sanction","arrest","investigation","money laundering","charged","convicted","lawsuit","fine","corruption"]
    queries = []
    for s in suffixes:
        base = (alias + s).strip()
        queries.append(base)
        for k in keywords:
            queries.append(f"{base} {k}")
            queries.append(f"{k} {base}")
    return list(dict.fromkeys([q for q in queries if q]))

# ---------- Smart fetch (aliases -> queries -> prioritized sources) ----------
def smart_fetch_entity(entity_name, per_source_limit=6, max_total=80, use_newsdata=True, use_aliasing=True, domain_priority_list=None):
    if not entity_name:
        return []

    # aliases from overrides, wiki, etc.
    raw_aliases = [entity_name]
    # manual overrides (exact match)
    if entity_name in alias_overrides:
        raw_aliases = [entity_name] + alias_overrides.get(entity_name, [])
    elif entity_name.lower() in (k.lower() for k in alias_overrides.keys()):
        # case-insensitive match
        for k in alias_overrides:
            if k.lower() == entity_name.lower():
                raw_aliases = [entity_name] + alias_overrides.get(k, [])
                break

    if use_aliasing:
        try:
            wiki_aliases = get_wikipedia_aliases(entity_name, max_aliases=6)
            for w in wiki_aliases:
                if w not in raw_aliases:
                    raw_aliases.append(w)
        except Exception:
            pass

    aliases = raw_aliases
    logger.info(f"Aliases used: {aliases}")

    # query bank
    query_bank = []
    for a in aliases:
        query_bank.extend(build_query_variations(a))
    query_bank = list(dict.fromkeys(query_bank))
    logger.info(f"Query bank size: {len(query_bank)}")

    results = []
    seen_links = set()

    def add_hits(source_name, hits):
        for h in hits:
            title = (h.get("title") or "").strip()
            summary = (h.get("summary") or "").strip()
            link = (h.get("link") or h.get("url") or "").strip()
            if not link:
                continue
            key = re.sub(r'\?.*$', '', link).rstrip('/')
            if key in seen_links:
                continue
            if is_relevant_to_aliases(aliases, title, summary):
                seen_links.add(key)
                results.append({"source": source_name, "title": title, "summary": summary, "link": link})
            if len(results) >= max_total:
                break

    # priority: newsdata -> ddg -> bing
    if use_newsdata and os.environ.get("NEWSDATA_KEY"):
        for q in query_bank[:12]:
            hits = newsdata_fetch(q, max_results=per_source_limit)
            add_hits("NewsData", hits)
            if len(results) >= max_total:
                break

    # DuckDuckGo for top aliases
    for a in aliases[:4]:
        hits = ddg_instant_search(a + " sanctions OR fraud OR investigation", max_results=per_source_limit)
        add_hits("DuckDuckGo", hits)
        if len(results) >= max_total:
            break

    # Bing for queries
    for q in query_bank[:24]:
        hits = bing_search(q, max_results=per_source_limit)
        add_hits("Bing", hits)
        if len(results) >= max_total:
            break

    # Apply domain priority: if enabled, move prioritized domains to top preserving order
    if domain_priority_list:
        def domain_score(item):
            try:
                hostname = urllib.parse.urlparse(item["link"]).hostname or ""
                for idx, d in enumerate(domain_priority_list):
                    if d in hostname:
                        return - (len(domain_priority_list) - idx)  # higher priority -> more negative to sort earlier
            except Exception:
                pass
            return 0
        results.sort(key=lambda it: domain_score(it))

    logger.info(f"Total relevant hits returned: {len(results)}")
    return results

# ---------- Main action ----------
if st.button("🔍 Scan Now"):
    if not entity or not entity.strip():
        st.warning("Enter an entity name to scan.")
        st.stop()

    with st.spinner("Running checks..."):
        t0 = time.time()

        # load current domain priority (file)
        domain_priority = load_json_file(DOMAIN_PRIORITY_FILE, [])

        sanctions_hits = check_opensanctions(entity.strip())

        hits = smart_fetch_entity(entity.strip(),
                                  per_source_limit=per_source_limit,
                                  max_total=120,
                                  use_newsdata=use_newsdata_checkbox,
                                  use_aliasing=advanced_aliasing,
                                  domain_priority_list=domain_priority if prioritize_domains_ui else None)

        rows = []
        for h in hits:
            text = (h.get("title","") or "") + " " + (h.get("summary","") or "")
            rows.append({
                "Source": h.get("source",""),
                "Title": h.get("title",""),
                "Summary": summarize(text, max_chars=600),
                "Risk Level": risk_level_from_text(text),
                "Link": h.get("link","")
            })
        df = pd.DataFrame(rows)
        t1 = time.time()

        st.subheader("Sanctions / Watchlist (OpenSanctions)")
        if sanctions_hits:
            st.error(f"Sanctions hits found: {len(sanctions_hits)} — HIGH RISK")
            for s in sanctions_hits:
                st.markdown(f"**{s.get('name')}** • _{s.get('type')}_ • Source: `{s.get('source')}`")
                if s.get("note"):
                    st.write(s.get("note")[:800])
                st.write("---")
        else:
            st.success("No quick OpenSanctions matches found.")

        st.subheader("Adverse Media / Mentions (combined)")
        if df.empty:
            st.warning("No adverse media hits found. Try alternate spellings, enable NewsData (NEWSDATA_KEY), or adjust alias overrides.")
            st.info("Manual debug links (open in browser):")
            st.write(f"- OpenSanctions: https://api.opensanctions.org/search?q={urllib.parse.quote(entity)}")
            st.write(f"- DuckDuckGo: https://api.duckduckgo.com/?q={urllib.parse.quote(entity + ' sanctions')}&format=json")
            st.write(f"- Bing (search page): https://www.bing.com/search?q={urllib.parse.quote(entity + ' sanctions')}")
        else:
            st.dataframe(df, height=520)
            st.subheader("Risk Distribution")
            fig, ax = plt.subplots()
            df["Risk Level"].value_counts().plot(kind="pie", autopct="%1.1f%%", ax=ax)
            st.pyplot(fig)
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv, "sam_radar_results.csv", "text/csv")

    st.caption(f"Scan finished in {t1 - t0:.2f}s. Check logs for details (NewsData / DuckDuckGo / Bing).")
