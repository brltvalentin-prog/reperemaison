"""RepèreMaison – version mobile (Streamlit). Saisie : commune, terrain, surface habitable."""
from argparse import Namespace

import streamlit as st
import streamlit.components.v1 as components

import repere_parcelles as rp

st.set_page_config(page_title="RepèreMaison", page_icon="🏠")
st.title("🏠 RepèreMaison")
st.caption("Trouve la parcelle d'une annonce dont l'adresse est masquée.")


@st.cache_data(show_spinner=False)
def calculer(commune, cp, terrain, habitable, tolerance):
    args = Namespace(commune=commune, cp=cp, insee=None, terrain=float(terrain), habitable=float(habitable),
                     tolerance=tolerance / 100, emprise_min=round(float(habitable) * 0.7),
                     no_open=True, max_resultats=60)
    page = rp.run(args, log=lambda *_: None)
    return page.read_text(encoding="utf-8")


with st.form("recherche"):
    commune = st.text_input("Commune", placeholder="ex. Nouzilly")
    cp = st.text_input("Code postal (facultatif)", placeholder="37380")
    c1, c2 = st.columns(2)
    terrain = c1.number_input("Terrain (m²)", min_value=0, step=10, value=0)
    habitable = c2.number_input("Surface habitable (m²)", min_value=0, step=5, value=0)
    tolerance = st.slider("Tolérance sur le terrain (%)", 5, 40, 15)
    texte = st.text_area("Ou colle le texte de l'annonce (remplit les champs vides)", height=100)
    ok = st.form_submit_button("Chercher les maisons", type="primary")

if ok:
    infos = rp.extraire_annonce(texte or "")
    commune = (commune or infos.get("commune") or "").strip()
    cp = (cp or infos.get("cp") or "").strip()
    terrain = terrain or infos.get("terrain")
    habitable = habitable or infos.get("habitable")
    manque = [n for n, v in (("commune", commune), ("terrain", terrain), ("surface habitable", habitable)) if not v]
    if manque:
        st.warning("Il manque : " + ", ".join(manque))
    else:
        try:
            with st.spinner("Analyse en cours (de quelques secondes à quelques minutes)…"):
                doc = calculer(commune, cp, terrain, habitable, tolerance)
            st.success(f"Résultats pour {commune}. Les parcelles rouges sont les meilleurs candidats.")
            components.html(doc, height=950, scrolling=True)
        except (RuntimeError, SystemExit) as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Erreur : {e}")
