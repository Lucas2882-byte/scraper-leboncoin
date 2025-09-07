# streamlit_app.py
# -------------------------------------------------------------
# Scraper & analyseur d'annonces (Leboncoin) pour repérer
# les bons deals composants/PC montés et estimer la marge.
#
# ⚠️ Avertissements juridiques/techniques :
# - Respecte les CGU/robots.txt de Leboncoin et les lois locales.
# - Utilise un rate limiting agressif (sleep) et des User-Agent variés.
# - Ce scraper est best-effort : la structure peut changer => mets à jour l’adapter si besoin.
# - Ce code n’est affilié à aucun service ; usage éducatif uniquement.
#
# Déploiement :
# - Déploie sur Streamlit Cloud avec requirements.txt + packages.txt + startup.sh
#   (voir README/mes instructions).
# -------------------------------------------------------------

import re
import time
import json
import random
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

import streamlit as st
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup

# Optionnel: Playwright pour contourner le rendu client/anti-bot
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except Exception:
    PLAYWRIGHT_OK = False

# ----------------------------
# Utilitaires
# ----------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
]

HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

CURRENCY = "€"

# ----------------------------
# Modèles de données
# ----------------------------

@dataclass
class Listing:
    source: str
    url: str
    title: str
    price_eur: Optional[float]
    location: Optional[str]
    date_str: Optional[str]
    seller: Optional[str] = None
    description: Optional[str] = None
    images: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    # Analyse
    detected_parts: Dict[str, int] = field(default_factory=dict)  # {part_key: qty}
    parts_value_eur: float = 0.0
    target_negotiation_pct: float = 0.0
    negotiated_price_eur: Optional[float] = None
    estimated_margin_eur: Optional[float] = None

# ----------------------------
# Détection de pièces (regex)
# ----------------------------

DEFAULT_PART_PATTERNS: Dict[str, str] = {
    # GPUs
    "gpu_rtx_3070": r"\brtx\s*3070\b|\b3070ti\b",
    "gpu_rtx_4060": r"\brtx\s*4060\b",
    "gpu_rx_5700_xt": r"\brx\s*5700\s*xt\b",
    # CPUs
    "cpu_ryzen_5_5600x": r"\b(ryzen\s*5\s*5600x)\b",
    "cpu_ryzen_5_3600": r"\b(ryzen\s*5\s*3600)\b",
    "cpu_i5_12400f": r"\bi5[- ]?12400f\b",
    # RAM/SSD/ALIM/CM
    "ram_ddr4_16": r"\b(16\s*go\s*ddr4|ddr4\s*16\s*go)\b",
    "ram_ddr4_32": r"\b(32\s*go\s*ddr4|ddr4\s*32\s*go)\b",
    "ssd_1to": r"\b(1\s*t[bo]|1to|1\s*tera)\b",
    "psu_650w_gold": r"\b(650w).*\b(gold)\b",
    "mobo_b450": r"\bb450\b",
}

# Valeurs de référence (réglables côté UI)
DEFAULT_PART_VALUES: Dict[str, int] = {
    "gpu_rtx_3070": 300,
    "gpu_rtx_4060": 260,
    "gpu_rx_5700_xt": 170,
    "cpu_ryzen_5_5600x": 140,
    "cpu_ryzen_5_3600": 70,
    "cpu_i5_12400f": 120,
    "ram_ddr4_16": 45,
    "ram_ddr4_32": 85,
    "ssd_1to": 55,
    "psu_650w_gold": 65,
    "mobo_b450": 55,
}

# ----------------------------
# Adapter Leboncoin
# ----------------------------

class LeboncoinAdapter:
    BASE_URL = "https://www.leboncoin.fr/recherche/"

    @staticmethod
    def build_search_url(query: str, category: Optional[str] = None, locations: Optional[str] = None,
                         price_min: Optional[int] = None, price_max: Optional[int] = None, page: int = 1) -> str:
        """Construit une URL de recherche simple."""
        params = {"text": query.strip(), "page": page}
        if price_min is not None:
            params["price"] = f"{price_min}-{price_max if price_max is not None else ''}"
        if locations:
            params["locations"] = locations
        q = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
        return f"{LeboncoinAdapter.BASE_URL}?{q}"

    # --- Récupération HTTP simple (souvent bloqué / HTML partiel) ---
    @staticmethod
    def fetch_page_requests(url: str, timeout: int = 25) -> Optional[str]:
        headers = HEADERS_BASE.copy()
        headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.text
            return None
        except requests.RequestException:
            return None

    # --- Récupération via navigateur headless (Playwright) ---
    @staticmethod
    def fetch_page_browser(url: str, wait_selector: Optional[str] = None, timeout_ms: int = 25000) -> Optional[str]:
        if not PLAYWRIGHT_OK:
            return None
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
                )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(url, wait_until="domcontentloaded")

                # Scroll pour déclencher le lazy-load des cartes
                for _ in range(6):
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(500)

                # Attendre un sélecteur s'il est fourni
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=6000)
                    except Exception:
                        pass

                html = page.content()
                context.close()
                browser.close()
                return html
        except Exception:
            return None

    @staticmethod
    def parse_listings(html: str) -> List[Listing]:
        """Essaye 2 stratégies :
        1) JSON __NEXT_DATA__ s'il existe (sites Next.js)
        2) Fallback : parse HTML via sélecteurs génériques
        """
        soup = BeautifulSoup(html, "html.parser")

        # 1) NEXT_DATA JSON (si dispo)
        script = soup.find("script", id="__NEXT_DATA__")
        listings: List[Listing] = []
        if script and script.text:
            try:
                data = json.loads(script.text)
                nodes = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("searchData", {})
                    .get("ads", [])
                )
                for ad in nodes:
                    title = ad.get("subject") or ad.get("title") or "(sans titre)"
                    url = ad.get("url") or ad.get("shareLink") or ""
                    price = ad.get("price") or ad.get("priceCents")
                    if isinstance(price, dict):
                        price = price.get("value")
                    if isinstance(price, (int, float)) and price and price > 10000:
                        price = price / 100  # cents -> €
                    price = float(price) if price else None
                    location = None
                    loc = ad.get("location") or {}
                    if isinstance(loc, dict):
                        location = loc.get("city") or loc.get("label")
                    date_str = ad.get("index_date") or ad.get("first_publication_date")
                    images = []
                    pics = ad.get("images", [])
                    if isinstance(pics, list):
                        images = [p.get("url") for p in pics if isinstance(p, dict) and p.get("url")]

                    listings.append(
                        Listing(
                            source="Leboncoin",
                            url=url,
                            title=title,
                            price_eur=price,
                            location=location,
                            date_str=date_str,
                            images=images,
                            raw=ad,
                        )
                    )
            except Exception:
                pass

        # 2) Fallback HTML générique
        if not listings:
            cards = soup.select("a[data-qa-id='aditem_container'], a.AdCard__Link, a.trackable")
            for a in cards:
                url = a.get("href", "")
                title_el = a.select_one("span, h2, h3")
                title = title_el.get_text(strip=True) if title_el else "(sans titre)"
                price = None
                price_el = a.find(text=re.compile(r"\d+\s*€"))
                if price_el:
                    price_num = re.sub(r"[^0-9]", "", price_el)
                    if price_num:
                        price = float(price_num)
                listings.append(
                    Listing(
                        source="Leboncoin",
                        url=("https://www.leboncoin.fr" + url) if url and url.startswith("/") else url,
                        title=title,
                        price_eur=price,
                        location=None,
                        date_str=None,
                    )
                )
        return listings

# ----------------------------
# Analyseur de pièces & marges
# ----------------------------

def detect_parts(text: str, patterns: Dict[str, str]) -> Dict[str, int]:
    found: Dict[str, int] = {}
    lower = text.lower()
    for key, pat in patterns.items():
        try:
            m = re.findall(pat, lower)
            if m:
                found[key] = len(m)
        except re.error:
            pass
    return found


def compute_parts_value(parts: Dict[str, int], price_map: Dict[str, float]) -> float:
    total = 0.0
    for k, qty in parts.items():
        total += price_map.get(k, 0) * qty
    return total


def estimate_margin(listing: Listing, negotiation_pct: float, dismantle_bonus_pct: float = 0.0) -> Listing:
    ask = listing.price_eur or np.nan
    negotiated = None if pd.isna(ask) else float(max(0, ask * (1.0 - negotiation_pct / 100.0)))
    parts_value = listing.parts_value_eur * (1.0 + dismantle_bonus_pct / 100.0)
    margin = None if negotiated is None else float(parts_value - negotiated)

    listing.negotiated_price_eur = negotiated
    listing.estimated_margin_eur = margin
    return listing

# ----------------------------
# UI Streamlit
# ----------------------------

st.set_page_config(page_title="Scraper marges LBC (GPU/CPU/PC complets)", layout="wide")
st.title("🔎 Scraper & Analyse de marge — Leboncoin (France)")

with st.sidebar:
    st.header("Paramètres de recherche")
    queries = st.text_input(
        "Mots-clés (séparés par des virgules)",
        value="RTX 3070, Ryzen 5 5600X, PC gamer, RX 5700 XT, i5-12400F, RTX 4060",
        help="Ex.: RTX 3070, Ryzen 5 5600X, PC gamer complet, etc.",
    )
    price_min = st.number_input("Prix min (€)", min_value=0, value=0, step=10)
    price_max = st.number_input("Prix max (€)", min_value=0, value=1500, step=50)
    pages = st.slider("Pages / requête", min_value=1, max_value=10, value=2)
    locations = st.text_input("Zone (optionnel)", value="France", help="Laisse 'France' pour national.")

    st.divider()
    st.header("Récupération")
    use_browser = st.toggle(
        "Mode navigateur (Playwright)",
        value=False,
        help="Utilise Chromium headless pour contourner le rendu client/anti-bot. Nécessite playwright/chromium installés.",
    )

    st.divider()
    st.header("Heuristiques d'analyse")
    with st.expander("Valeur de référence par pièce (€) — éditable"):
        price_map = DEFAULT_PART_VALUES.copy()
        for k in list(price_map.keys()):
            price_map[k] = st.number_input(k, value=int(price_map[k]), step=5)
    with st.expander("Patterns de détection (regex) — avancé"):
        patterns = DEFAULT_PART_PATTERNS.copy()
        pat_json = st.text_area("Regex JSON", value=json.dumps(patterns, indent=2), height=240)
        try:
            patterns = json.loads(pat_json)
        except json.JSONDecodeError:
            st.warning("JSON invalide — utilisation des patterns par défaut.")
            patterns = DEFAULT_PART_PATTERNS

    negotiation_pct = st.slider("Hypothèse de négociation (%)", 0, 30, 10)
    dismantle_bonus_pct = st.slider(
        "Bonus 'démontage' vs revente pièces (%)", 0, 30, 5,
        help="Prime appliquée à la valeur pièces si tu démontes/revends séparément"
    )

    st.divider()
    throttle = st.slider("Délai entre pages (s)", 0.5, 5.0, 1.2, step=0.1)

# Boutons d'action
colA, colB, colC = st.columns([1, 1, 1])
start = colA.button("Lancer la recherche")
export_btn = colB.button("Exporter CSV")

# Espace résultats
results_container = st.container()

# Mémoire de session
if "results_df" not in st.session_state:
    st.session_state["results_df"] = pd.DataFrame()

# ----------------------------
# Moteur de recherche
# ----------------------------

def run_search() -> pd.DataFrame:
    all_listings: List[Listing] = []
    qlist = [q.strip() for q in queries.split(",") if q.strip()]

    for q in qlist:
        for page in range(1, pages + 1):
            url = LeboncoinAdapter.build_search_url(
                query=q,
                locations=locations or None,
                price_min=price_min if price_min else None,
                price_max=price_max if price_max else None,
                page=page,
            )
            with st.spinner(f"{q} — page {page} …"):
                if use_browser:
                    html = LeboncoinAdapter.fetch_page_browser(
                        url, wait_selector="a[data-qa-id='aditem_container']"
                    )
                    if not html:
                        html = LeboncoinAdapter.fetch_page_requests(url)
                else:
                    html = LeboncoinAdapter.fetch_page_requests(url)

            if not html:
                st.info(f"Aucune donnée récupérée pour {q} page {page} (peut être bloqué/CGU)")
                time.sleep(throttle)
                continue

            listings = LeboncoinAdapter.parse_listings(html)
            for lst in listings:
                # Filtre prix
                if lst.price_eur is not None:
                    if price_min and lst.price_eur < price_min:
                        continue
                    if price_max and lst.price_eur > price_max:
                        continue
                text = f"{lst.title}\n{lst.description or ''}"
                parts = detect_parts(text, patterns)
                lst.detected_parts = parts
                lst.parts_value_eur = compute_parts_value(parts, price_map)
                lst.target_negotiation_pct = negotiation_pct
                lst = estimate_margin(lst, negotiation_pct, dismantle_bonus_pct)
                all_listings.append(lst)

            time.sleep(throttle)

    # Vers DataFrame
    rows = []
    for L in all_listings:
        rows.append({
            "source": L.source,
            "title": L.title,
            "price": L.price_eur,
            "negotiated_price": L.negotiated_price_eur,
            "parts_value": round(L.parts_value_eur, 2),
            "est_margin": None if L.estimated_margin_eur is None else round(L.estimated_margin_eur, 2),
            "margin_%": None if (L.estimated_margin_eur is None or not L.negotiated_price_eur) else round(100.0 * L.estimated_margin_eur / max(1.0, L.negotiated_price_eur), 1),
            "location": L.location,
            "date": L.date_str,
            "url": L.url,
            "detected_parts": ", ".join([f"{k}×{v}" for k, v in L.detected_parts.items()]) if L.detected_parts else "",
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["est_margin"], ascending=[False])
    return df

# ----------------------------
# Actions
# ----------------------------

if start:
    df = run_search()
    st.session_state["results_df"] = df

if export_btn:
    df = st.session_state.get("results_df", pd.DataFrame())
    if df.empty:
        st.warning("Aucun résultat à exporter.")
    else:
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Télécharger le CSV",
            data=csv,
            file_name="resultats_scraper_leboncoin.csv",
            mime="text/csv",
        )

# ----------------------------
# Affichage des résultats
# ----------------------------

with results_container:
    df = st.session_state.get("results_df", pd.DataFrame())
    st.subheader("Résultats")
    if df.empty:
        st.info("Lance une recherche pour voir les annonces classées par marge estimée.")
    else:
        # Filtres rapides
        min_margin = st.slider("Filtrer marge min (€)", 0, int(df["est_margin"].fillna(0).max() or 0), 0)
        only_positive = st.checkbox("Uniquement marges positives", value=True)
        df_view = df.copy()
        df_view = df_view[df_view["est_margin"].fillna(-1) >= min_margin]
        if only_positive:
            df_view = df_view[df_view["est_margin"].fillna(-1) > 0]

        st.dataframe(
            df_view,
            use_container_width=True,
            hide_index=True,
            column_config={
                "url": st.column_config.LinkColumn("Annonce"),
                "price": st.column_config.NumberColumn("Prix (€)", format="%d"),
                "negotiated_price": st.column_config.NumberColumn("Prix négocié (€)", format="%d"),
                "parts_value": st.column_config.NumberColumn("Valeur pièces (€)", format="%d"),
                "est_margin": st.column_config.NumberColumn("Marge estimée (€)", format="%d"),
                "margin_%": st.column_config.NumberColumn("Marge (%)", format="%.1f"),
            },
        )

# ----------------------------
# Exemple de requirements.txt (met dans un fichier séparé)
# ----------------------------
# streamlit
# requests
# beautifulsoup4
# lxml
# pandas
# numpy
# playwright
