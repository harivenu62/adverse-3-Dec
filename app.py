# app.py
"""
SAM-Radar (clean) — OpenSanctions + NewsData (optional) + DuckDuckGo + Bing
No admin UI. Includes built-in alias seeds for common problematic entities (e.g. Litasco, Vijay Mallya).
"""

import os
import time
import logging
import sys
import urllib.parse
import re
import difflib

import requests
from bs4 import BeautifulSoup
import feedparser
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

# --- Logging so Render Live Tail shows messages ---
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sam-radar")

HEADERS = {"User-Agent": "sam-radar-bot/1.0 (+https://example.com)"}

st.set_page_config(page_title="SAM-Radar (clean)", layout="wide")
st.title("🛰️ SAM-Radar — Adverse Media & Sanctions (clean)")
st.caption("OpenSanctions + NewsData (optional) + DuckDuckGo + Bing. Built-in alias seeds for common entities.")

# ------------------------------
# Small built-in alias seed map
# ------------------------------
# Add common problematic names here so queries are stronger out-of-the-box.
BUILTIN_ALIAS_SEEDS = {
    "litasco": ["Litasco", "Litasco SA", "LUKOIL Litasco", "Lukoil Litasco"],
    "vijay mallya": ["Vijay Mallya", "Vijay M. Mallya", "Vijay Mallya (businessman)", "Kingfisher Airlines Vijay Mallya"],
    "kubair mullchandi": ["Kubair Mullchandi", "K. Mullchandi"],  # example; add more as needed
    "lukoil": ["Lukoil", "PJSC Lukoil", "LUKOIL"],
}

# ------------------------------
# UI inputs
# ------------------------------
entity = st.text_input("Company or Individual name", placeholder="e.g. Litasco, Vijay Mallya, Lukoil")
per_source_limit = st.slider("Max results per source (per query)", 1, 12, 6)
use_newsdata_checkbox = st.checkbox("Use NewsData.io fallback (set NEWSDATA_KEY in env)", value=True)
use_wikipedia_aliasing = st.checkbox("Use Wikipedia alias expansion (optional)", value=False)

# ------------------------------
# Helpers: summarizer + risk
# ------------------------------
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

# ------------------------------
# Wikipedia aliases (best-effort)
# ------------------------------
def get_wikipedia_aliases(name, max_aliases=5):
    aliases = []
    try:
        q = urllib.parse.quote(name)
        search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json"
        r = requests.get(search_url, headers=HEADERS, timeout=8)
        if r.ok:
            data = r.json()
            hits = data.get("query", {}).get("search", [])[:5]
            for h in hits:
                title = h.get("title")
                if title and title not in aliases:
                    aliases.append(title)
    except Exception:
        logger.exception("Wikipedia alias fetch failed")
    return aliases[:max_aliases]

# ------------------------------
# OpenSanctions check
# ------------------------------
def check_opensanctions(name):
    results = []
    if not name:
        return results
    try:
        url = f"https://api.opensanctions.org/search?q={urllib.parse.quote(name)}"
        r = requests.get(url, headers=HEADERS, timeout=12)
        logger.info(f"OpenSanctions status: {r.status_code} for '{name}'")
        if r.ok:
            data = r.json()
            hits = data.get("results") or data.get("data") or []
            for h in hits[:8]:
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

# ------------------------------
# DuckDuckGo Instant
# ------------------------------
def ddg_instant_search(q, max_results=6):
    out = []
    try:
        params = {"q": q, "format": "json", "no_html": 1, "skip_disambig": 1}
        r = requests.get("https://api.duckduckgo.com/", params=params, headers=HEADERS, timeout=12)
        logger.info(f"DDG status: {r.status_code} for '{q}'")
        if r.ok:
            data = r.json()
            if data.get("AbstractText"):
                out.append({"source": "DuckDuckGo", "title": data.get("Heading", q), "summary": data.get("AbstractText"), "link": data.get("AbstractURL", "")})
            for it in (data.get("Results") or [])[:max_results]:
                out.append({"source": "DuckDuckGo", "title": it.get("Text"), "summary": "", "link": it.get("FirstURL")})
            for topic in (data.get("RelatedTopics") or [])[:max_results]:
                if isinstance(topic, dict):
                    text = topic.get("Text") or topic.get("Name")
                    url = topic.get("FirstURL")
                    if text and url:
                        out.append({"source": "DuckDuckGo", "title": text, "summary": "", "link": url})
    except Exception:
        logger.exception("DuckDuckGo instant failed")
    # dedupe
    seen = set()
    dedup = []
    for item in out:
        key = (item.get("link") or "") + "|" + (item.get("title") or "")
        if key not in seen and item.get("link"):
            dedup.append(item)
            seen.add(key)
        if len(dedup) >= max_results:
            break
    return dedup

# ------------------------------
# NewsData fetch (optional)
# ------------------------------
def newsdata_fetch(query, max_results=6):
    out = []
    key = os.environ.get("NEWSDATA_KEY", "").strip()
    if not key:
        logger.info("NewsData key not set; skipping NewsData.")
        return out
    try:
        url = f"https://newsdata.io/api/1/news?apikey={key}&q={urllib.parse.quote(query)}&language=en"
        r = requests.get(url, headers=HEADERS, timeout=12)
        logger.info(f"NewsData status: {r.status_code} for '{query}'")
        if r.ok:
            data = r.json()
            for a in data.get("results", [])[:max_results]:
                out.append({"source": "NewsData", "title": a.get("title"), "summary": a.get("description") or a.get("content",""), "link": a.get("link")})
    except Exception:
        logger.exception("NewsData fetch failed")
    return out

# ------------------------------
# Bing lightweight scraping
# ------------------------------
def bing_search(query, max_results=6):
    out = []
    try:
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=HEADERS, timeout=12)
        logger.info(f"Bing status: {r.status_code} for '{query}' (len={len(r.text)})")
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

# ------------------------------
# Relevance helpers
# ------------------------------
def is_relevant(aliases, title, summary):
    txt = ((title or "") + " " + (summary or "")).lower()
    # exact alias word match
    for a in aliases:
        if re.search(r'\b' + re.escape(a.lower()) + r'\b', txt):
            return True
    # fuzzy title similarity
    for a in aliases:
        if difflib.SequenceMatcher(None, a.lower(), (title or "").lower()).ratio() >= 0.78:
            return True
    # requires at least one adverse keyword for broader matches
    adverse = ["fraud","sanction","arrest","investigation","scam","money laundering","charged","lawsuit","convicted","corruption"]
    if any(k in txt for k in adverse):
        return True
    return False

# ------------------------------
# Query builder
# ------------------------------
def build_queries_from_alias(alias):
    alias = alias.strip()
    suffixes = ["", " plc", " ltd", " llc", " inc", " corp", " group", " holdings", " sa"]
    keywords = ["fraud","scam","sanction","arrest","investigation","money laundering","charged","lawsuit","convicted","corruption"]
    q = []
    for s in suffixes:
        base = (alias + s).strip()
        q.append(base)
        for k in keywords:
            q.append(f"{base} {k}")
            q.append(f"{k} {base}")
    # dedupe keep order
    return list(dict.fromkeys(q))

# ------------------------------
# Smart fetch that prioritizes NewsData -> DDG -> Bing
# ------------------------------
def smart_fetch(entity_name, per_source_limit=6, max_total=80, use_newsdata=True, use_wiki_aliasing=False):
    if not entity_name:
        return []

    name_l = entity_name.strip().lower()
    # start with builtin alias seeds if present
    aliases = [entity_name.strip()]
    if name_l in BUILTIN_ALIAS_SEEDS:
        for a in BUILTIN_ALIAS_SEEDS[name_l]:
            if a not in aliases:
                aliases.append(a)

    # optional wikipedia aliases
    if use_wiki_aliasing:
        try:
            wiki_aliases = get_wikipedia_aliases(entity_name, max_aliases=5)
            for w in wiki_aliases:
                if w not in aliases:
                    aliases.append(w)
        except Exception:
            pass

    logger.info(f"Aliases used: {aliases}")

    query_bank = []
    for a in aliases:
        query_bank.extend(build_queries_from_alias(a))
    query_bank = list(dict.fromkeys(query_bank))
    logger.info(f"Query bank size: {len(query_bank)}")

    results = []
    seen = set()

    def add_hits(src, hits):
        for h in hits:
            title = (h.get("title") or "").strip()
            summary = (h.get("summary") or "").strip()
            link = (h.get("link") or h.get("url") or "").strip()
            if not link:
                continue
            key = re.sub(r'\?.*$', '', link).rstrip('/')
            if key in seen:
                continue
            if is_relevant(aliases, title, summary):
                seen.add(key)
                results.append({"source": src, "title": title, "summary": summary, "link": link})
            if len(results) >= max_total:
                break

    # NewsData priority (if key present)
    nd_key = os.environ.get("NEWSDATA_KEY", "").strip()
    if use_newsdata and nd_key:
        for q in query_bank[:12]:
            hits = newsdata_fetch(q, max_results=per_source_limit)
            add_hits("NewsData", hits)
            if len(results) >= max_total:
                break

    # DuckDuckGo for top aliases
    for a in aliases[:4]:
        q = f"{a} sanctions OR fraud OR investigation"
        hits = ddg_instant_search(q, max_results=per_source_limit)
        add_hits("DuckDuckGo", hits)
        if len(results) >= max_total:
            break

    # Bing fallback for queries
    for q in query_bank[:24]:
        hits = bing_search(q, max_results=per_source_limit)
        add_hits("Bing", hits)
        if len(results) >= max_total:
            break

    logger.info(f"smart_fetch returned {len(results)} hits")
    return results

# ------------------------------
# Main action
# ------------------------------
if st.button("🔍 Scan Now"):
    if not entity or not entity.strip():
        st.warning("Enter an entity name.")
        st.stop()

    with st.spinner("Running OpenSanctions check and multi-source fetch..."):
        start = time.time()

        # OpenSanctions quick check
        sanctions = check_opensanctions(entity.strip())

        # fetch combined news/mentions
        hits = smart_fetch(entity.strip(), per_source_limit=per_source_limit,
                           max_total=120, use_newsdata=use_newsdata_checkbox,
                           use_wiki_aliasing=use_wikipedia_aliasing)

        rows = []
        for h in hits:
            txt = (h.get("title","") or "") + " " + (h.get("summary","") or "")
            rows.append({
                "Source": h.get("source",""),
                "Title": h.get("title",""),
                "Summary": summarize(txt, max_chars=600),
                "Risk Level": risk_level_from_text(txt),
                "Link": h.get("link","")
            })
        df = pd.DataFrame(rows)

        elapsed = time.time() - start
        logger.info(f"Scan done in {elapsed:.2f}s. Sanctions={len(sanctions)}, Hits={len(df)}")

        # display sanctions
        st.subheader("Sanctions / Watchlist (OpenSanctions)")
        if sanctions:
            st.error(f"Sanctions hits found: {len(sanctions)} — treat as HIGH RISK")
            for s in sanctions:
                st.markdown(f"**{s.get('name')}** • _{s.get('type')}_ • Source: `{s.get('source')}`")
                if s.get("note"):
                    st.write(s.get("note")[:800])
                st.write("---")
        else:
            st.success("No quick OpenSanctions matches found.")

        # display adverse media
        st.subheader("Adverse Media / Mentions")
        if df.empty:
            st.warning("No adverse media hits found. Try alternate spellings, enable NewsData, or toggle Wikipedia alias expansion.")
            # helpful debug quick checks
            st.info("Manual quick-check links (open in browser):")
            st.write(f"- OpenSanctions: https://api.opensanctions.org/search?q={urllib.parse.quote(entity)}")
            st.write(f"- DuckDuckGo Instant: https://api.duckduckgo.com/?q={urllib.parse.quote(entity + ' sanctions')}&format=json")
            st.write(f"- Bing search: https://www.bing.com/search?q={urllib.parse.quote(entity + ' sanctions')}")
        else:
            st.dataframe(df, height=520)
            st.subheader("Risk Distribution")
            fig, ax = plt.subplots()
            df["Risk Level"].value_counts().plot(kind="pie", autopct="%1.1f%%", ax=ax)
            st.pyplot(fig)
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv, "sam_radar_results.csv", "text/csv")

    st.caption(f"Scan finished in {elapsed:.2f}s. Check logs for details (NewsData/DDG/Bing statuses).")
