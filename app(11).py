# streamlit_app.py
# -------------------------------------------------------------
# App simple : Ville + Rayon (km) + Mot-clé -> annonces Leboncoin
# Deux modes :
#   1) Simple (requests) — le plus léger
#   2) Navigateur (Playwright) — si la page nécessite JS
# -------------------------------------------------------------

import os
import re
import json
import time
import random
from typing import Optional, List, Dict

import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup

# =============== Options ===============

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
]
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

LBC_SEARCH_BASE = "https://www.leboncoin.fr/recherche/"

# =============== Utils ===============

def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    R = 6371.0
    dlat = radians(lat2-lat1)
    dlon = radians(lon2-lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    c = 2*asin(sqrt(a))
    return R*c

def geocode_city(city: str) -> Optional[Dict]:
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": city, "format": "json", "limit": 1, "countrycodes": "fr"},
            headers={"User-Agent": "lbc-simple/1.0"},
            timeout=15,
        )
        if r.status_code == 200 and r.json():
            item = r.json()[0]
            return {"lat": float(item["lat"]), "lon": float(item["lon"]), "display_name": item.get("display_name")}
    except Exception:
        return None
    return None

def build_search_url(query: str, locations: Optional[str], page: int) -> str:
    from urllib.parse import quote_plus
    params = [f"text={quote_plus(query.strip())}", f"page={page}"]
    if locations and locations.strip():
        params.append(f"locations={quote_plus(locations.strip())}")
    return f"{LBC_SEARCH_BASE}?{'&'.join(params)}"

# =============== Fetchers ===============

def fetch_requests(url: str, timeout: int = 25) -> Optional[str]:
    headers = HEADERS.copy()
    headers["User-Agent"] = random.choice(USER_AGENTS)
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.text
        return None
    except Exception:
        return None

def ensure_chromium_if_needed():
    # Essaie d'importer playwright. Si absent, on ne fait rien ici (mode simple fonctionnera quand même).
    try:
        import subprocess
        import importlib
        importlib.import_module("playwright")
        # Installe le navigateur si pas déjà présent (sans --with-deps)
        browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/home/appuser/.cache/ms-playwright")
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", browsers_path)
        chromium_dir = os.path.join(browsers_path, "chromium")
        if not os.path.exists(chromium_dir):
            subprocess.run(["python", "-m", "playwright", "install", "chromium"],
                           check=True, capture_output=True, text=True)
        return True, None
    except Exception as e:
        return False, str(e)

def fetch_playwright(url: str, timeout_ms: int = 25000) -> Optional[str]:
    ok, err = ensure_chromium_if_needed()
    if not ok:
        st.warning("Playwright indisponible : " + (err or ""))
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 2000},
                java_script_enabled=True,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(url, wait_until="domcontentloaded")
            for _ in range(8):
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(400)
            html = page.content()
            context.close()
            browser.close()
            return html
    except Exception as e:
        st.warning(f"Playwright a échoué: {e}")
        return None

# =============== Parsing ===============

def parse_ads(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict] = []
    # 1) Next.js JSON
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.text:
        try:
            data = json.loads(script.text)
            ads = data.get("props", {}).get("pageProps", {}).get("searchData", {}).get("ads", [])
            for ad in ads:
                title = ad.get("subject") or ad.get("title") or "(sans titre)"
                url = ad.get("url") or ad.get("shareLink") or ""
                price = ad.get("price") or ad.get("priceCents")
                if isinstance(price, dict): price = price.get("value")
                if isinstance(price, (int, float)) and price and price > 10000: price = price/100
                price = float(price) if price else None
                loc = ad.get("location") or {}
                city = loc.get("city") or loc.get("label")
                lat = loc.get("lat") or loc.get("latitude")
                lon = loc.get("lng") or loc.get("longitude")
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
                date_str = ad.get("index_date") or ad.get("first_publication_date")
                out.append({
                    "titre": title, "prix (€)": price, "ville": city, "date": date_str,
                    "url": url, "lat": lat, "lon": lon
                })
            if out:
                return out
        except Exception:
            pass
    # 2) Fallback HTML
    cards = soup.select("a[data-qa-id='aditem_container'], a.AdCard__Link, a.trackable")
    for a in cards:
        url = a.get("href", "")
        if url.startswith("/"):
            url = "https://www.leboncoin.fr" + url
        t = a.select_one("span, h2, h3")
        title = t.get_text(strip=True) if t else "(sans titre)"
        txt = a.get_text(" ", strip=True)
        price = None
        m = re.search(r"(\d[\d\s]{0,9})\s*€", txt)
        if m:
            try:
                price = float(m.group(1).replace(" ", ""))
            except Exception:
                price = None
        out.append({"titre": title, "prix (€)": price, "ville": None, "date": None, "url": url, "lat": None, "lon": None})
    return out

# =============== UI ===============

st.set_page_config(page_title="Leboncoin — Ville/Rayon/Mot-clé", layout="wide")
st.title("🔎 Leboncoin — Ville + Rayon (km) + Mot-clé (SIMPLE)")

with st.sidebar:
    city = st.text_input("Ville (ex: Chartres)", value="Chartres")
    radius_km = st.number_input("Rayon (km)", min_value=1, max_value=200, value=20, step=1)
    keyword = st.text_input("Mot-clé", value="RTX 3060")
    pages = st.slider("Pages à parcourir", 1, 10, 2)
    mode = st.radio("Mode de récupération", ["Simple (requests)", "Navigateur (Playwright)"], index=0)
    throttle = st.slider("Délai entre pages (s)", 0.5, 5.0, 1.0, step=0.1)

col_run, col_csv = st.columns([1,1])
run = col_run.button("Chercher")
export = col_csv.button("Exporter CSV")

if "df" not in st.session_state:
    st.session_state["df"] = pd.DataFrame()

def do_fetch(url: str) -> Optional[str]:
    if mode.startswith("Navigateur"):
        return fetch_playwright(url)
    else:
        return fetch_requests(url)

def build_url(query: str, city_text: str, page: int) -> str:
    return build_search_url(query, city_text, page)

if run:
    geo = geocode_city(city)
    if not geo:
        st.error("Ville introuvable. Essaie un libellé plus précis (ex: 'Chartres, 28000').")
    else:
        lat0, lon0 = geo["lat"], geo["lon"]
        st.success(f"Centre: {city} = {lat0:.4f}, {lon0:.4f}")
        rows: List[Dict] = []
        for p in range(1, pages+1):
            url = build_url(keyword, city, p)
            with st.spinner(f"Page {p} …"):
                html = do_fetch(url)
            if not html:
                st.info(f"Pas de contenu récupéré pour la page {p}.")
                time.sleep(throttle)
                continue
            ads = parse_ads(html)
            # calc distance si coords dispo
            for ad in ads:
                d = None
                if ad.get("lat") is not None and ad.get("lon") is not None:
                    d = haversine_km(lat0, lon0, ad["lat"], ad["lon"])
                ad["distance (km)"] = round(d, 1) if d is not None else None
                rows.append(ad)
            time.sleep(throttle)

        df = pd.DataFrame(rows)
        if not df.empty:
            # Filtre par rayon si distance connue (si non connue, on laisse au cas où)
            keep = df["distance (km)"].isna() | (df["distance (km)"] <= radius_km)
            df = df[keep].drop_duplicates(subset=["url"])
            # Tri: distance puis prix si dispo
            sort_cols, asc = [], []
            if "distance (km)" in df.columns: sort_cols.append("distance (km)"); asc.append(True)
            if "prix (€)" in df.columns: sort_cols.append("prix (€)"); asc.append(True)
            if sort_cols: df = df.sort_values(sort_cols, ascending=asc)
        st.session_state["df"] = df

df = st.session_state["df"]
st.subheader("Résultats")
if df.empty:
    st.info("Aucun résultat. Lance une recherche.")
else:
    show_cols = [c for c in ["titre","prix (€)","ville","distance (km)","date","url"] if c in df.columns]
    st.dataframe(df[show_cols], use_container_width=True, hide_index=True)
    with st.expander("Liste cliquable"):
        for _, r in df.iterrows():
            st.write(f"- [{r.get('titre')}]({r.get('url')}) — {r.get('prix (€)')} € — {r.get('ville')} — {r.get('distance (km)')} km")

if export:
    df = st.session_state.get("df", pd.DataFrame())
    if df.empty:
        st.warning("Rien à exporter.")
    else:
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Télécharger CSV", data=csv, file_name="annonces_lbc.csv", mime="text/csv")
