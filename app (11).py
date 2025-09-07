# streamlit_app.py
# -------------------------------------------------------------
# Mini-app : récupérer les annonces Leboncoin par mot-clé + zone
# (titres, prix, lieu, date, url, image) et afficher/exporter.
#
# Dépendances :
#   requirements.txt -> streamlit, playwright, beautifulsoup4, lxml, pandas
#   startup.sh       -> python -m playwright install --with-deps chromium
#   packages.txt     -> libnss3 libxss1 libasound2 libgbm1  (Streamlit Cloud)
# -------------------------------------------------------------

import json
import random
import time
from typing import List, Optional, Dict

import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup

# Playwright pour rendre la page et bypass le JS
from playwright.sync_api import sync_playwright

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
]

LBC_SEARCH_BASE = "https://www.leboncoin.fr/recherche/"


def build_search_url(query: str, locations: Optional[str], page: int) -> str:
    # On reste simple : text + page + locations (France, région, ville…)
    # Leboncoin accepte locations=France (texte libre) pour une recherche large
    from urllib.parse import quote_plus
    params = [f"text={quote_plus(query.strip())}", f"page={page}"]
    if locations and locations.strip():
        params.append(f"locations={quote_plus(locations.strip())}")
    return f"{LBC_SEARCH_BASE}?{'&'.join(params)}"


def fetch_html_with_browser(url: str, timeout_ms: int = 25000) -> Optional[str]:
    """Ouvre l'URL avec Chromium headless, scrolle pour charger les cards, renvoie le HTML."""
    try:
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
                viewport={"width": 1280, "height": 1800},
                java_script_enabled=True,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(url, wait_until="domcontentloaded")

            # Scroll progressif pour déclencher le lazy-load
            for _ in range(8):
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(500)

            # Attendre au moins 1 carte si possible (ne bloque pas si absent)
            try:
                page.wait_for_selector("a[data-qa-id='aditem_container']", timeout=5000)
            except Exception:
                pass

            html = page.content()
            context.close()
            browser.close()
            return html
    except Exception as e:
        st.warning(f"Playwright a échoué: {e}")
        return None


def parse_listings_from_html(html: str) -> List[Dict]:
    """Deux voies: JSON __NEXT_DATA__ (quand dispo) sinon parse HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict] = []

    # 1) JSON Next.js (plus propre)
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.text:
        try:
            data = json.loads(script.text)
            ads = (
                data.get("props", {})
                .get("pageProps", {})
                .get("searchData", {})
                .get("ads", [])
            )
            for ad in ads:
                title = ad.get("subject") or ad.get("title") or "(sans titre)"
                url = ad.get("url") or ad.get("shareLink") or ""
                # prix
                price = ad.get("price") or ad.get("priceCents")
                if isinstance(price, dict):
                    price = price.get("value")
                if isinstance(price, (int, float)) and price and price > 10000:
                    price = price / 100  # cents -> €
                price = float(price) if price else None
                # ville / date
                loc = ad.get("location") or {}
                location = loc.get("city") or loc.get("label")
                date_str = ad.get("index_date") or ad.get("first_publication_date")
                # image
                img = None
                pics = ad.get("images", [])
                if isinstance(pics, list) and pics:
                    img = pics[0].get("url")
                results.append(
                    {
                        "titre": title,
                        "prix (€)": price,
                        "lieu": location,
                        "date": date_str,
                        "url": url,
                        "image": img,
                    }
                )
            if results:
                return results
        except Exception:
            pass

    # 2) Fallback HTML
    cards = soup.select("a[data-qa-id='aditem_container'], a.AdCard__Link, a.trackable")
    for a in cards:
        url = a.get("href", "")
        if url.startswith("/"):
            url = "https://www.leboncoin.fr" + url
        # titre
        t = a.select_one("span, h2, h3")
        title = t.get_text(strip=True) if t else "(sans titre)"
        # prix
        price_txt = a.get_text(" ", strip=True)
        import re as _re
        price = None
        m = _re.search(r"(\d[\d\s]{0,9})\s*€", price_txt)
        if m:
            try:
                price = float(m.group(1).replace(" ", ""))
            except Exception:
                price = None
        # image (si miniatures)
        img = None
        img_el = a.select_one("img")
        if img_el and img_el.get("src"):
            img = img_el.get("src")
        results.append({"titre": title, "prix (€)": price, "lieu": None, "date": None, "url": url, "image": img})

    return results


# =========================  UI  =========================

st.set_page_config(page_title="Leboncoin — Annonces (simple)", layout="wide")
st.title("🔎 Leboncoin — Récupération d’annonces (simple)")

with st.sidebar:
    query = st.text_input("Mot-clé", value="RTX 3070")
    zone = st.text_input("Zone (ex: France, Paris, Lyon…)", value="France")
    pages = st.slider("Pages à parcourir", 1, 10, 2)
    throttle = st.slider("Délai entre pages (s)", 0.5, 5.0, 1.2, step=0.1)

col_run, col_csv = st.columns([1, 1])
run = col_run.button("Lancer")
export = col_csv.button("Exporter CSV")

if "df" not in st.session_state:
    st.session_state["df"] = pd.DataFrame()

if run:
    all_rows: List[Dict] = []
    for page in range(1, pages + 1):
        url = build_search_url(query, zone, page)
        with st.spinner(f"Chargement {query} — page {page}"):
            html = fetch_html_with_browser(url)
        if not html:
            st.info(f"Pas de contenu récupéré pour la page {page}.")
            time.sleep(throttle)
            continue
        items = parse_listings_from_html(html)
        all_rows.extend(items)
        time.sleep(throttle)

    df = pd.DataFrame(all_rows)
    # Nettoyage / tri basique
    if not df.empty:
        df = df.drop_duplicates(subset=["url"])
        # colonnes utiles d'abord
        cols = [c for c in ["titre", "prix (€)", "lieu", "date", "url", "image"] if c in df.columns]
        df = df[cols]
        # Tri par prix si dispo
        if "prix (€)" in df.columns and df["prix (€)"].notna().any():
            df = df.sort_values("prix (€)", ascending=True, na_position="last")
    st.session_state["df"] = df

df = st.session_state["df"]
st.subheader("Résultats")
if df.empty:
    st.info("Aucun résultat pour l’instant. Lance une recherche.")
else:
    # Affichage tableau + mini vignettes (si images)
    # On affiche l'URL comme lien
    show = df.copy()
    # Streamlit sait rendre les colonnes 'url' en clic via st.dataframe? -> non, on laisse texte cliquable via st.link_button plus bas
    st.dataframe(show, use_container_width=True, hide_index=True)
    # Liste cliquable compacte
    with st.expander("Voir la liste cliquable"):
        for _, row in df.iterrows():
            st.write(f"- [{row.get('titre')}]({row.get('url')}) — {row.get('prix (€)')} € — {row.get('lieu')}")

if export:
    df = st.session_state.get("df", pd.DataFrame())
    if df.empty:
        st.warning("Rien à exporter.")
    else:
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Télécharger CSV", data=csv, file_name="leboncoin_annonces.csv", mime="text/csv")
