# app.py
# SAM-Radar (OpenSanctions + DuckDuckGo Instant Answer + Bing fallback)
#
# Requirements (put into requirements.txt):
# streamlit
# requests
# pandas
# beautifulsoup4
# lxml
#
# Paste this file into your repo root as app.py and deploy (Render/Railway/local).
# Optional: set NEWSDATA_KEY env var to enable NewsData fallback (not required).
# ------------------------------------------------------------------------------

import os
import time
import logging
import sys
import urllib.parse
from typing import List, Dict

import requests
from bs4 import BeautifulSoup
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

# logging (so Render / platform logs show useful messages)
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sam-radar")

# Streamlit page config
st.set_page_config(page_title="SAM-Radar (OpenSanctions + DDG + Bing)", layout="wide")
st.title("🛰️ SAM-Radar — OpenSanctions + DuckDuckGo + Bing (Adverse Media & Sanctions)")

# UI inputs
entity = st.text_input("Company or Individual name", placeholder="e.g. Lukoil, Binance, Adani Group")
limit = st.slider("Max results per source", 1, 20, 8)
use_newsdata = st.checkbox("Use NewsData.io fallback (set NEWSDATA_KEY in env)", value=False)

st.markdown("**Note:** OpenSanctions is used for sanctions/watchlist checks (no API key required). DuckDuckGo Instant Answer is used to collect quick links/summaries. Bing search is used as a fallback (light scraping).")

# -------------------------
# Utility helpers
# -------------------------
HEADERS = {"User-Agent": "sam-radar-bot/1.0 (+https://example.com)"}


def safe_get(url: str, params: dict = None, timeout: int = 12) -> requests.Response:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        logger.info(f"GET {r.url} -> {r.status_code}")
        return r
    except Exception as e:
        logger.exception(f"Request failed: {url} ({e})")
        raise


# -------------------------
# 1) OpenSanctions - sanctions / watchlist check
# -------------------------
def check_opensanctions(name: str) -> List[Dict]:
    """
    Query OpenSanctions search API. Returns list of matched sanction/watchlist entities.
    """
    results = []
    if not name:
        return results

    try:
        q = urllib.parse.quote(name)
        url = f"https://api.opensanctions.org/search?q={q}"
        r = safe_get(url, timeout=10)
        if r.status_code != 200:
            logger.warning("OpenSanctions returned non-200")
            return results
        payload = r.json()
        # payload may contain 'results' or 'data' depending on API surface; handle generically
        hits = payload.get("results") or payload.get("data") or payload.get("entities") or []
        for h in hits[:limit]:
            # structure may differ; try common fields
            name_hit = h.get("name") or h.get("title") or h.get("label") or ""
            nation = h.get("countries") or h.get("country") or h.get("addresses") or []
            source = h.get("source") or h.get("sources") or ""
            # build readable entry
            results.append({
                "name": name_hit,
                "type": h.get("schema", h.get("type", "entity")),
                "source": source,
                "descr": h.get("notes") or h.get("summary") or "",
                "raw": h
            })
        logger.info(f"OpenSanctions hits: {len(results)}")
    except Exception:
        logger.exception("OpenSanctions check failed")
    return results


# -------------------------
# 2) DuckDuckGo Instant Answer (JSON) - quick result aggregation
# -------------------------
def ddg_instant(entity_name: str, max_results: int = 8) -> List[Dict]:
    """
    Uses DuckDuckGo Instant Answer API to fetch related topics and top links.
    Returns list of simplified articles.
    """
    out = []
    if not entity_name:
        return out
    try:
        q = entity_name + " sanctions OR fraud OR corruption OR investigation"
        params = {"q": q, "format": "json", "no_html": 1, "skip_disambig": 1}
        url = "https://api.duckduckgo.com/"
        r = safe_get(url, params=params, timeout=10)
        if r.status_code != 200:
            return out
        data = r.json()
        # Primary abstract text (if any)
        if data.get("AbstractText"):
            out.append({
                "source": "DuckDuckGo (abstract)",
                "title": data.get("Heading", entity_name),
                "summary": data.get("AbstractText"),
                "link": data.get("AbstractURL", "")
            })
        # RelatedTopics often contains nested results
        topics = data.get("RelatedTopics", []) or []
        for topic in topics:
            if isinstance(topic, dict):
                # sometimes topic contains 'Text' and 'FirstURL'
                title = topic.get("Text") or topic.get("Name") or ""
                link = topic.get("FirstURL") or ""
                if title and link:
                    out.append({"source": "DuckDuckGo (related)", "title": title, "summary": "", "link": link})
            elif isinstance(topic, list):
                for t in topic[:max_results]:
                    title = t.get("Text") or ""
                    link = t.get("FirstURL") or ""
                    if title and link:
                        out.append({"source": "DuckDuckGo (related)", "title": title, "summary": "", "link": link})
        # also check 'Results' key for top results
        for ritem in (data.get("Results") or [])[:max_results]:
            out.append({"source": "DuckDuckGo (result)", "title": ritem.get("Text"), "summary": "", "link": ritem.get("FirstURL")})
        logger.info(f"DDG Instant returned {len(out)} items")
    except Exception:
        logger.exception("DDG Instant failed")
    # dedupe by link/title
    seen = set()
    dedup = []
    for it in out:
        key = (it.get("link") or "") + "|" + (it.get("title") or "")
        if key not in seen:
            dedup.append(it)
            seen.add(key)
        if len(dedup) >= max_results:
            break
    return dedup


# -------------------------
# 3) Bing lightweight fallback (HTML parse)
# -------------------------
def bing_search(entity_name: str, max_results: int = 8) -> List[Dict]:
    """
    Light Bing search scraping. Not heavy - only parse search results page to extract titles/links/snippets.
    """
    out = []
    if not entity_name:
        return out
    try:
        q = urllib.parse.quote(entity_name + " sanctions OR fraud OR investigation OR corruption")
        url = f"https://www.bing.com/search?q={q}&form=QBLH"
        r = safe_get(url, timeout=12)
        if r.status_code != 200:
            return out
        soup = BeautifulSoup(r.text, "lxml")
        results = soup.select("li.b_algo")
        for res in results[:max_results]:
            h2 = res.find("h2")
            title = h2.get_text(strip=True) if h2 else ""
            link_tag = h2.find("a") if h2 else None
            link = link_tag["href"] if link_tag and link_tag.has_attr("href") else ""
            snippet = ""
            s = res.select_one("p")
            if s:
                snippet = s.get_text(strip=True)
            out.append({"source": "Bing", "title": title, "summary": snippet, "link": link})
        logger.info(f"Bing returned {len(out)} items")
    except Exception:
        logger.exception("Bing scrape failed")
    return out


# -------------------------
# 4) Optional NewsData.io fallback (requires NEWSDATA_KEY env var)
# -------------------------
def newsdata_fetch(entity_name: str, max_results: int = 8) -> List[Dict]:
    out = []
    key = os.environ.get("NEWSDATA_KEY", "").strip()
    if not key or not entity_name:
        return out
    try:
        q = urllib.parse.quote(entity_name + " sanctions OR fraud OR crime")
        url = f"https://newsdata.io/api/1/news?apikey={key}&q={q}&language=en"
        r = safe_get(url, timeout=12)
        if r.status_code != 200:
            return out
        data = r.json()
        for a in data.get("results", [])[:max_results]:
            out.append({"source": "NewsData", "title": a.get("title"), "summary": a.get("description") or a.get("content", ""), "link": a.get("link")})
        logger.info(f"NewsData returned {len(out)} items")
    except Exception:
        logger.exception("NewsData fetch failed")
    return out


# -------------------------
# Aggregation & scoring
# -------------------------
def risk_score_from_text(text: str) -> str:
    if not text:
        return "Low"
    t = text.lower()
    high_kw = ["fraud", "money laundering", "sanction", "arrest", "convicted", "charged", "embezzle", "bribery", "terror"]
    med_kw = ["investigation", "probe", "regulatory", "lawsuit", "review"]
    score = 0
    for k in high_kw:
        if k in t:
            score += 2
    for k in med_kw:
        if k in t:
            score += 1
    if score >= 2:
        return "High"
    if score == 1:
        return "Medium"
    return "Low"


# -------------------------
# Main action UI
# -------------------------
if st.button("Scan Now"):
    if not entity or not entity.strip():
        st.warning("Enter an entity name first.")
        st.stop()

    with st.spinner("Querying OpenSanctions + DuckDuckGo + Bing (and fallback)..."):
        start = time.time()

        # 1. Sanctions check
        sanctions = check_opensanctions(entity.strip())
        sanctioned = len(sanctions) > 0

        # 2. DuckDuckGo Instant Answer results
        ddg_results = ddg_instant(entity, limit)

        # 3. Bing fallback
        bing_results = bing_search(entity, limit)

        # 4. Optional NewsData
        nd_results = newsdata_fetch(entity, limit)

        combined = []
        # unify structure
        for r in ddg_results + bing_results + nd_results:
            title = r.get("title") or ""
            summary = r.get("summary") or ""
            link = r.get("link") or ""
            src = r.get("source") or ""
            combined.append({
                "Source": src,
                "Title": title,
                "Summary": summary,
                "Risk Level": risk_score_from_text((title + " " + summary)),
                "Link": link
            })

        df = pd.DataFrame(combined)

        elapsed = time.time() - start
        logger.info(f"Total hits combined: {len(df)} (elapsed {elapsed:.2f}s). Sanctions hits: {len(sanctions)}")

        # --- Display sanctions / watchlist results ---
        st.subheader("Sanctions / Watchlist Check (OpenSanctions)")
        if sanctioned:
            st.error(f"Sanctions hits found: {len(sanctions)} — treat as HIGH RISK")
            # list top hits
            for s in sanctions:
                st.markdown(f"**{s.get('name')}** • _{s.get('type')}_ • Source: `{s.get('source')}`")
                descr = s.get("descr") or ""
                if descr:
                    st.write(descr[:700])
        else:
            st.success("No sanctions/watchlist matches found in OpenSanctions search (quick check).")

        # --- Display adverse media results ---
        st.subheader("Adverse Media / Public Mentions")
        if df.empty:
            st.warning("No adverse media results found from DuckDuckGo/Bing/NewsData for this query.")
            st.info("Tips: try alternative spellings, include jurisdiction, or add 'plc'/'llc' (e.g. 'Lukoil plc').")
        else:
            # show table
            st.dataframe(df, height=420)

            # risk distribution chart
            st.subheader("Risk Distribution")
            fig, ax = plt.subplots()
            df["Risk Level"].value_counts().plot(kind="pie", autopct="%1.1f%%", ax=ax)
            st.pyplot(fig)

            # download CSV
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, "sam_radar_results.csv", "text/csv")

    st.caption(f"Query completed in {elapsed:.2f}s. Logs include HTTP results for debugging.")

# Footer info and quick test links
st.markdown("---")
st.markdown("""
**Developer notes / testing tips**

- OpenSanctions: `https://api.opensanctions.org/search?q=<name>`  
- DuckDuckGo Instant: `https://api.duckduckgo.com/?q=<query>&format=json`  
- Bing: lightweight HTML parsing of search results (no JS).
- For highest reliability, add a NewsData.io API key and set it as `NEWSDATA_KEY` in your host environment — NewsData provides more news coverage for sanctions stories.
""")
