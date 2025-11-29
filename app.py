# app.py (Render-ready SAM-Radar)
import streamlit as st
import feedparser
import requests
import pandas as pd
import urllib.parse
import matplotlib.pyplot as plt
import time
import sys
import logging

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

st.set_page_config(page_title="SAM-Radar", layout="wide")
st.title("🛰️ SAM-Radar — Smart Adverse Media Radar")

entity = st.text_input("Company or Individual Name")
limit = st.slider("Number of articles to fetch (per source)", 3, 20, 8)

# --- GNews (demo token) ---
def gnews_fetch(entity, limit=10):
    if not entity:
        return []
    try:
        q = urllib.parse.quote(entity + " sanctions fraud crime")
        url = f"https://gnews.io/api/v4/search?q={q}&token=demo&max={limit}"
        r = requests.get(url, timeout=15)
        logger.info(f"GNews status: {r.status_code}")
        data = r.json()
        out = []
        for a in data.get("articles", [])[:limit]:
            out.append({
                "source": "GNews",
                "title": a.get("title",""),
                "summary": a.get("description",""),
                "link": a.get("url",""),
                "published": a.get("publishedAt","N/A")
            })
        logger.info(f"GNews returned {len(out)} articles")
        return out
    except Exception as e:
        logger.exception("GNews fetch failed")
        return []

# --- NewsLookup RSS ---
def rss_fetch(entity, limit=10):
    if not entity:
        return []
    try:
        q = urllib.parse.quote(entity + " sanctions fraud crime")
        url = f"https://newslookup.com/rss/search?q={q}"
        feed = feedparser.parse(url)
        out = []
        for entry in feed.entries[:limit]:
            out.append({
                "source": "NewsLookup",
                "title": entry.get("title",""),
                "summary": entry.get("summary",""),
                "link": entry.get("link",""),
                "published": entry.get("published","N/A")
            })
        logger.info(f"NewsLookup returned {len(out)} articles")
        return out
    except Exception:
        logger.exception("NewsLookup fetch failed")
        return []

def risk_level(text):
    t = (text or "").lower()
    high = ["fraud","money laundering","scam","crime","corruption","sanction","arrest"]
    medium = ["investigation","probe","regulatory","review"]
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

if st.button("🔍 Scan Now"):
    if not entity:
        st.warning("Please enter an entity name.")
        st.stop()

    with st.spinner("Scanning sources..."):
        t0 = time.time()
        g1 = gnews_fetch(entity, limit)
        g2 = rss_fetch(entity, limit)
        combined = g1 + g2
        logger.info(f"Total combined articles: {len(combined)} (GNews={len(g1)}, NewsLookup={len(g2)})")

        processed = []
        for a in combined:
            text = (a.get("title","") + " " + a.get("summary","")).strip()
            processed.append({
                "Source": a.get("source",""),
                "Title": a.get("title",""),
                "Summary": text[:500],
                "Risk Level": risk_level(text),
                "Published": a.get("published",""),
                "Link": a.get("link","")
            })
        df = pd.DataFrame(processed)
        t1 = time.time()
        logger.info(f"Processing done in {t1-t0:.2f}s, rows={len(df)}")

        st.subheader("Results")
        if df.empty:
            st.warning("No articles found. Try alternative spellings or try again later.")
            st.write("Log messages (for debugging):")
            st.write(f"• GNews returned: {len(g1)} articles")
            st.write(f"• NewsLookup returned: {len(g2)} articles")
            st.write("If both are zero and you are on a hosted platform, the host may be blocking outbound requests.")
        else:
            st.dataframe(df, height=500)

            st.subheader("Risk Chart")
            fig, ax = plt.subplots()
            df["Risk Level"].value_counts().plot(kind="pie", autopct="%1.1f%%", ax=ax)
            st.pyplot(fig)

            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv, "SAM-Radar-Report.csv", "text/csv")
