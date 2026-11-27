"""RepèreMaison – version mobile (Streamlit). Cadastre + diagnostics énergétiques (DPE)."""
from argparse import Namespace

import requests
import streamlit as st
import streamlit.components.v1 as components

import repere_parcelles as rp

st.set_page_config(page_title="RepèreMaison", page_icon="🏠")
st.title("🏠 RepèreMaison")
st.caption("Trouve la parcelle (et l'adresse probable) d'une annonce dont l'adresse est masquée.")


@st.cache_data(ttl=3600, show_spinner=False)
def chercher_communes(saisie):
    """Suggestions de communes d'après un code postal (5 chiffres) ou le début d'un nom."""
    saisie = (saisie or "").strip()
    params = {"fields": "nom,code,codesPostaux,departement", "limit": 10}
    if saisie.isdigit():
        if len(saisie) != 5:
            return []
        params["codePostal"] = saisie
    elif len(saisie) >= 2:
        params.update(nom=saisie, boost="population")
    else:
        return []
    try:
        r = requests.get("https://geo.api.gouv.fr/communes", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def calculer(commune, cp, insee, terrain, habitable, tolerance, classe, maison, hab_tol, dpe):
    args = Namespace(commune=commune, cp=cp, insee=insee or None, terrain=float(terrain), habitable=float(habitable),
                     tolerance=tolerance / 100, emprise_min=round(float(habitable) * 0.7),
                     classe=classe, type_bien="maison" if maison else "", hab_tol=hab_tol / 100,
                     utiliser_dpe=dpe, no_open=True, max_resultats=60)
    page = rp.run(args, log=lambda *_: None)
    return page.read_text(encoding="utf-8")


def libelle(c):
    cps = ", ".join(c.get("codesPostaux", [])[:2])
    dep = (c.get("departement") or {}).get("nom", "")
    return f"{c['nom']} ({cps}) – {dep}"


saisie = st.text_input("Code postal ou nom de la commune", placeholder="ex. 37380 ou Nouzilly",
                       help="Tape le code postal (5 chiffres) ou le début du nom, puis valide : les communes proposées s'affichent.")
choix = None
resultats = chercher_communes(saisie)
if resultats:
    i = st.selectbox("Choisis la commune", range(len(resultats)), format_func=lambda k: libelle(resultats[k]))
    choix = resultats[i]
elif saisie.strip():
    st.caption("Aucune commune trouvée : vérifie le code postal (5 chiffres) ou l'orthographe.")

with st.form("recherche"):
    c1, c2 = st.columns(2)
    terrain = c1.number_input("Terrain (m²)", min_value=0, step=10, value=0)
    habitable = c2.number_input("Surface habitable (m²)", min_value=0, step=5, value=0)
    classe = st.selectbox("Classe énergie de l'annonce (DPE)", ["", "A", "B", "C", "D", "E", "F", "G"],
                          format_func=lambda x: x or "Je ne sais pas")
    with st.expander("Options"):
        dpe = st.checkbox("Chercher aussi les adresses via les DPE", value=True)
        maison = st.checkbox("C'est une maison (pas un appartement)", value=True)
        tolerance = st.slider("Tolérance sur le terrain (%) — 0 = surface identique au cadastre", 0, 40, 15)
        hab_tol = st.slider("Tolérance sur la surface habitable (%)", 2, 20, 8)
        texte = st.text_area("Ou colle le texte de l'annonce (remplit les champs vides)", height=100)
    ok = st.form_submit_button("Chercher les maisons", type="primary")

if ok:
    infos = rp.extraire_annonce(texte or "")
    if choix:
        commune, insee = choix["nom"], choix["code"]
        cp = saisie.strip() if saisie.strip().isdigit() else (choix.get("codesPostaux") or [""])[0]
    else:
        commune, insee, cp = (infos.get("commune") or "").strip(), "", (infos.get("cp") or "").strip()
    terrain = terrain or infos.get("terrain")
    habitable = habitable or infos.get("habitable")
    classe = classe or infos.get("classe", "")
    manque = [n for n, v in (("commune", commune), ("terrain", terrain), ("surface habitable", habitable)) if not v]
    if manque:
        st.warning("Il manque : " + ", ".join(manque))
    else:
        try:
            with st.spinner("Analyse en cours (de quelques secondes à quelques minutes)…"):
                doc = calculer(commune, cp, insee, terrain, habitable, tolerance, classe, maison, hab_tol, dpe)
            st.success(f"Résultats pour {commune} : une seule liste, avec un indice global. "
                       "Change de méthode (global, cadastre seul, DPE seul) au-dessus du tableau.")
            components.html(doc, height=1000, scrolling=True)
        except (RuntimeError, SystemExit) as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Erreur : {e}")
