#!/usr/bin/env python3
"""
RepèreMaison – recherche de parcelles candidates dans une commune.

Utilise uniquement des données publiques (cadastre Etalab + geo.api.gouv.fr).
Pour la commune donnée, liste les parcelles dont la surface est proche de celle
de l'annonce ET qui portent un grand bâtiment, puis les classe par score.

Exemple (annonce de Nouzilly, valeurs par défaut) :
    python repere_parcelles.py
Autre annonce :
    python repere_parcelles.py --commune "Aubagne" --cp 13400 --terrain 800 --habitable 120
"""
import argparse, csv, gzip, html, json, re, sys, webbrowser
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform
from shapely.strtree import STRtree

BASE = "https://cadastre.data.gouv.fr/data/etalab-cadastre/latest/geojson/communes"
TO_L93 = Transformer.from_crs(4326, 2154, always_xy=True).transform
CACHE = Path(__file__).parent / "cache"
OUT = Path(__file__).parent / "resultats"


def trouver_commune(nom, cp):
    r = requests.get("https://geo.api.gouv.fr/communes",
                     params={"nom": nom, "codePostal": cp, "fields": "nom,code,codeDepartement",
                             "limit": 5}, timeout=30)
    r.raise_for_status()
    res = r.json()
    if not res:  # certains codes postaux ne matchent pas : on réessaie par le nom seul
        r = requests.get("https://geo.api.gouv.fr/communes",
                         params={"nom": nom, "fields": "nom,code,codeDepartement", "limit": 5},
                         timeout=30)
        r.raise_for_status()
        res = r.json()
    if not res:
        sys.exit(f"Commune introuvable : {nom}. Utilise --insee pour donner le code INSEE.")
    if len(res) > 1:
        print("Plusieurs communes trouvées, la première est retenue :",
              ", ".join(f"{c['nom']} ({c['code']})" for c in res))
    return res[0]["code"], res[0]["codeDepartement"], res[0]["nom"]


def fichier_local(insee, couche):
    """Cherche un fichier .json.gz déjà téléchargé (cache, dossier du script, Téléchargements)."""
    nom = f"cadastre-{insee}-{couche}.json.gz"
    for d in (CACHE, Path(__file__).parent, Path.home() / "Downloads"):
        if (d / nom).exists():
            return d / nom
    return None


def lire_gz(chemin):
    brut = chemin.read_bytes()
    try:
        brut = gzip.decompress(brut)
    except OSError:
        pass  # déjà décompressé par le navigateur
    return json.loads(brut.decode("utf-8"))


def charger(insee, dep, couche):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{insee}-{couche}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    manuel = fichier_local(insee, couche)
    if manuel:
        print(f"Lecture du fichier local : {manuel}")
        return lire_gz(manuel)
    url = f"{BASE}/{dep}/{insee}/cadastre-{insee}-{couche}.json.gz"
    print(f"Téléchargement : {url}")
    try:
        r = requests.get(url, timeout=180)
        r.raise_for_status()
    except requests.exceptions.RequestException:
        manquants = [c for c in ("parcelles", "batiments")
                     if not fichier_local(insee, c) and not (CACHE / f"{insee}-{c}.json").exists()]
        for c in manquants:
            webbrowser.open(f"{BASE}/{dep}/{insee}/cadastre-{insee}-{c}.json.gz")
        raise RuntimeError(
            "Le téléchargement automatique est bloqué par ton réseau. J'ai ouvert "
            f"{len(manquants)} téléchargement(s) dans ton navigateur : quand les fichiers sont dans "
            "ton dossier Téléchargements, clique de nouveau sur le bouton.")
    try:
        brut = gzip.decompress(r.content)
    except OSError:
        brut = r.content
    f.write_bytes(brut)
    return json.loads(brut.decode("utf-8"))


def borne(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def analyser(args, parcelles, batiments):
    bats = []
    for ft in batiments["features"]:
        if (ft.get("properties") or {}).get("type") not in (None, "01"):
            continue  # on ignore les bâtiments « légers » (abris, serres…)
        try:
            bats.append(transform(TO_L93, shape(ft["geometry"])))
        except Exception:
            continue
    arbre = STRtree(bats)
    cible = args.terrain
    emprise_attendue = args.habitable * 1.1  # plain-pied : emprise ≈ surface habitable + garage
    out = []
    for ft in parcelles["features"]:
        p = ft["properties"]
        try:
            g_wgs = shape(ft["geometry"])
            g = transform(TO_L93, g_wgs)
        except Exception:
            continue
        surf = p.get("contenance") or g.area
        if abs(surf - cible) > cible * args.tolerance:
            continue
        dedans = []
        for i in arbre.query(g, predicate="intersects"):
            b = bats[i]
            if b.area and b.intersection(g).area / b.area > 0.5:
                dedans.append(b)
        if not dedans:
            continue
        principal = max(dedans, key=lambda b: b.area)
        if principal.area < args.emprise_min:
            continue
        remplissage = principal.area / principal.minimum_rotated_rectangle.area
        s_surf = 1 - borne(abs(surf - cible) / (cible * args.tolerance))
        s_emp = 1 - borne(abs(principal.area - emprise_attendue) / (emprise_attendue * 0.5))
        s_forme = borne((0.92 - remplissage) / 0.25)  # L/U > rectangle
        score = round(100 * (0.4 * s_surf + 0.4 * s_emp + 0.2 * s_forme))
        c = g_wgs.centroid
        out.append({
            "parcelle": p.get("id", ""), "terrain_m2": int(surf),
            "emprise_principale_m2": int(principal.area),
            "nb_batiments": len(dedans), "forme_rectangle_pct": int(remplissage * 100),
            "score": score, "lat": round(c.y, 6), "lon": round(c.x, 6),
            "geom": mapping(g_wgs),
        })
    out.sort(key=lambda r: -r["score"])
    return out


def liens(lat, lon):
    return {
        "Satellite": f"https://www.google.com/maps/@{lat},{lon},19z/data=!3m1!1e3",
        "Street View": f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lon}",
        "Géoportail": f"https://www.geoportail.gouv.fr/carte?c={lon},{lat}&z=19&permalink=yes",
    }


def ecrire(res, nom, args):
    OUT.mkdir(exist_ok=True)
    with open(OUT / "candidats.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rang", "parcelle", "terrain_m2", "emprise_principale_m2", "nb_batiments",
                    "forme_rectangle_pct", "score", "lat", "lon", "satellite", "street_view"])
        for i, r in enumerate(res, 1):
            l = liens(r["lat"], r["lon"])
            w.writerow([i, r["parcelle"], r["terrain_m2"], r["emprise_principale_m2"],
                        r["nb_batiments"], r["forme_rectangle_pct"], r["score"], r["lat"],
                        r["lon"], l["Satellite"], l["Street View"]])
    feats, lignes = [], []
    for i, r in enumerate(res, 1):
        l = liens(r["lat"], r["lon"])
        a = " · ".join(f'<a href="{u}" target="_blank" rel="noopener">{k}</a>' for k, u in l.items())
        info = (f"<b>#{i} – score {r['score']}</b><br>{html.escape(r['parcelle'])}<br>"
                f"Terrain {r['terrain_m2']} m² · bâtiment {r['emprise_principale_m2']} m²<br>{a}")
        feats.append({"type": "Feature", "geometry": r["geom"],
                      "properties": {"popup": info, "score": r["score"]}})
        lignes.append(f"<tr><td>{i}</td><td>{html.escape(r['parcelle'])}</td><td>{r['terrain_m2']}</td>"
                      f"<td>{r['emprise_principale_m2']}</td><td>{r['forme_rectangle_pct']} %</td>"
                      f"<td><b>{r['score']}</b></td><td>{a}</td></tr>")
    data = json.dumps({"type": "FeatureCollection", "features": feats}).replace("</", "<\\/")
    page = PAGE.replace("__DATA__", data).replace("__ROWS__", "\n".join(lignes)) \
               .replace("__TITRE__", html.escape(f"{nom} – {len(res)} candidats")) \
               .replace("__CRIT__", html.escape(f"Terrain ≈ {args.terrain} m² (±{int(args.tolerance*100)} %), "
                                                f"habitable ≈ {args.habitable} m²"))
    (OUT / "candidats.html").write_text(page, encoding="utf-8")
    return OUT / "candidats.html"


PAGE = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITRE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{font:15px system-ui,sans-serif;margin:0}#map{height:55vh}main{padding:12px 16px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}</style>
</head><body><div id="map"></div><main><h2>__TITRE__</h2><p>__CRIT__. Clique une parcelle sur la carte
ou un lien du tableau, puis compare avec les photos de l’annonce.</p>
<table><tr><th>#</th><th>Parcelle</th><th>Terrain m²</th><th>Bâtiment m²</th><th>Forme</th><th>Score</th><th>Liens</th></tr>
__ROWS__</table></main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var data=__DATA__;
var ortho=L.tileLayer('https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg',{maxZoom:19,attribution:'© IGN'});
var osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'});
var map=L.map('map',{layers:[ortho]});L.control.layers({'Photo aérienne IGN':ortho,'Plan OSM':osm}).addTo(map);
var layer=L.geoJSON(data,{style:function(f){return{color:f.properties.score>=70?'#e11d48':'#f59e0b',weight:2,fillOpacity:.15}},
onEachFeature:function(f,l){l.bindPopup(f.properties.popup)}}).addTo(map);
if(data.features.length){map.fitBounds(layer.getBounds())}else{map.setView([46.6,2.4],6)}
</script></body></html>"""


def extraire_annonce(texte):
    """Devine commune, code postal, terrain et surface habitable dans un texte d'annonce collé."""
    res = {}
    nombres = []
    for m in re.finditer(r"(\d[\d\s\u202f\xa0]{0,8}?)\s*m[²2]", texte):
        n = int(re.sub(r"\D", "", m.group(1)) or 0)
        nombres.append((n, texte[max(0, m.start() - 40):m.start()].lower()))
    for n, ctx in nombres:
        if "terrain" in ctx or "parcelle" in ctx:
            res["terrain"] = n
            break
    for n, ctx in nombres:
        if 20 <= n <= 1000 and "terrain" not in ctx and "parcelle" not in ctx:
            res["habitable"] = n
            break
    for m in re.finditer(r"([A-Za-zÀ-ÿ'’\- ]{2,40}?)[\s,(]*\b(\d{5})\b(?![-\d])", texte):
        nom = m.group(1).strip(" -,(")
        if nom and not nom.lower().startswith(("ref", "réf")):
            res["commune"], res["cp"] = nom, m.group(2)
            break
    return res


def run(args, log=print):
    """Lance toute la recherche et retourne le chemin de la page de résultats."""
    if args.insee:
        insee = args.insee
        dep = insee[:3] if insee[:2] in ("97", "98") else insee[:2]
        nom = args.commune
    else:
        insee, dep, nom = trouver_commune(args.commune, args.cp)
    log(f"Commune : {nom} (INSEE {insee})")
    parcelles = charger(insee, dep, "parcelles")
    batiments = charger(insee, dep, "batiments")
    log(f"{len(parcelles['features'])} parcelles, {len(batiments['features'])} bâtiments analysés…")
    res = analyser(args, parcelles, batiments)
    if not res:
        raise RuntimeError("Aucun candidat. Élargis la tolérance ou baisse la taille minimale du bâtiment.")
    res = res[:getattr(args, "max_resultats", 60)]
    page = ecrire(res, nom, args)
    log(f"{len(res)} candidats retenus.")
    return page


def main():
    ap = argparse.ArgumentParser(description="Parcelles candidates d'après une annonce immobilière.")
    ap.add_argument("--commune", default="Nouzilly")
    ap.add_argument("--cp", default="37380", help="code postal")
    ap.add_argument("--insee", help="code INSEE (évite la recherche automatique)")
    ap.add_argument("--terrain", type=float, default=1251, help="surface du terrain (m²)")
    ap.add_argument("--habitable", type=float, default=195, help="surface habitable (m²)")
    ap.add_argument("--tolerance", type=float, default=0.15, help="écart toléré sur le terrain (0.15 = ±15 %%)")
    ap.add_argument("--emprise-min", dest="emprise_min", type=float, default=140,
                    help="emprise minimale du bâtiment principal (m²)")
    ap.add_argument("--no-open", action="store_true", help="ne pas ouvrir le navigateur")
    args = ap.parse_args()
    try:
        page = run(args)
    except RuntimeError as e:
        sys.exit(str(e))
    if not args.no_open:
        webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
