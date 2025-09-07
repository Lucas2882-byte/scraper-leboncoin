# Leboncoin — Ville + Rayon + Mot-clé (App simple)

## Fichiers
- `streamlit_app.py` : l'app Streamlit
- `requirements.txt` : dépendances Python (inclut Playwright)
- `packages.txt` : libs système pour Chromium (Cloud)
- `README.md` : ce fichier

## Déploiement — Streamlit Cloud
1. Python **3.11** (dans App settings → General).
2. Dépose ces 4 fichiers **à la racine** du repo GitHub.
3. Déploie. Au premier run, Playwright téléchargera Chromium automatiquement.
4. Si Playwright ne marche pas chez toi, utilise le mode **Simple (requests)** dans la sidebar.

## Local (recommandé pour tester)
```bash
pip install -r requirements.txt
python -m playwright install chromium
streamlit run streamlit_app.py
```
