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
import argparse, csv, gzip, html, json, re, sys, unicodedata, webbrowser
from pathlib import Path
from urllib.parse import quote

import requests
from pyproj import Transformer
from shapely.geometry import Point, shape, mapping
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


def score_terrain(surf, cible, tol):
    """1.0 si la surface du cadastre est identique à celle de l'annonce, puis décroissant."""
    ecart = abs(surf - cible)
    if ecart <= 0.5:
        return 1.0
    return 1 - borne((ecart - 0.5) / (cible * max(tol, 0.02)))


def score_cadastre(r):
    poids = [(r.get("s_terrain"), 0.4), (r.get("s_emprise"), 0.4), (r.get("s_forme"), 0.2)]
    dispo = [(v, w) for v, w in poids if v is not None]
    if not dispo:
        return 0
    return round(100 * sum(v * w for v, w in dispo) / sum(w for _, w in dispo))


def score_global(s_cad, s_dpe):
    """Indice unique : 55 % cadastre + 45 % DPE ; si un seul des deux existe, il compte pour 75 %."""
    if s_cad is not None and s_dpe is not None:
        return round(0.55 * s_cad + 0.45 * s_dpe)
    if s_cad is not None:
        return round(0.75 * s_cad)
    if s_dpe is not None:
        return round(0.75 * s_dpe)
    return 0


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
        if abs(surf - cible) > cible * args.tolerance + 0.5:
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
        s_surf = score_terrain(surf, cible, args.tolerance)
        s_emp = 1 - borne(abs(principal.area - emprise_attendue) / (emprise_attendue * 0.5))
        s_forme = borne((0.92 - remplissage) / 0.25)  # L/U > rectangle
        c = g_wgs.centroid
        out.append({
            "parcelle": p.get("id", ""), "terrain_m2": int(surf),
            "emprise_principale_m2": int(principal.area),
            "nb_batiments": len(dedans), "forme_rectangle_pct": int(remplissage * 100),
            "s_terrain": s_surf, "s_emprise": s_emp, "s_forme": s_forme,
            "lat": round(c.y, 6), "lon": round(c.x, 6), "geom": mapping(g_wgs),
        })
    return out


def liens(lat, lon):
    return {
        "Satellite": f"https://www.google.com/maps/@{lat},{lon},19z/data=!3m1!1e3",
        "Street View": f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lon}",
        "Géoportail": f"https://www.geoportail.gouv.fr/carte?c={lon},{lat}&z=19&permalink=yes",
    }


DPE_URL = "https://data.ademe.fr/data-fair/api/v1/datasets/dpe03existant/lines"
DPE_CHAMPS = ("numero_dpe,etiquette_dpe,date_etablissement_dpe,surface_habitable_logement,"
              "annee_construction,type_batiment,adresse_ban,code_postal_ban,nom_commune_ban")
GEOCODE_URL = "https://data.geopf.fr/geocodage/search"


def norm(t):
    t = unicodedata.normalize("NFD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


def codes_postaux(insee):
    try:
        r = requests.get(f"https://geo.api.gouv.fr/communes/{insee}", params={"fields": "codesPostaux"}, timeout=30)
        r.raise_for_status()
        return r.json().get("codesPostaux", [])
    except Exception:
        return []


def telecharger_dpe(cps, log):
    rows = []
    for cp in cps:
        url, params, pages = DPE_URL, {"code_postal_ban_in": cp, "select": DPE_CHAMPS, "size": 10000}, 0
        while url and pages < 6:
            r = requests.get(url, params=params if pages == 0 else None, timeout=120)
            r.raise_for_status()
            j = r.json()
            if not j.get("results"):
                break
            rows += j["results"]
            url = j.get("next")
            pages += 1
        log(f"DPE {cp} : {len(rows)} diagnostics lus")
    return rows


def chercher_dpe(args, insee, nom, log):
    """Adresses dont le DPE (ADEME) a une surface habitable proche de celle de l'annonce."""
    hab = float(args.habitable)
    tol = max(hab * getattr(args, "hab_tol", 0.08), 5)
    cps = set(codes_postaux(insee))
    if args.cp:
        cps.add(str(args.cp))
    if not cps:
        return []
    rows = telecharger_dpe(sorted(cps), log)
    n = norm(nom)
    rows = [r for r in rows if norm(r.get("nom_commune_ban")) == n] or rows
    classe = (getattr(args, "classe", "") or "").upper()
    maison = getattr(args, "type_bien", "maison") == "maison"

    def choisir(filtre_type):
        best = {}
        for r in rows:
            try:
                s = float(r.get("surface_habitable_logement"))
            except (TypeError, ValueError):
                continue
            if abs(s - hab) > tol or not r.get("adresse_ban"):
                continue
            if filtre_type and not str(r.get("type_batiment", "")).lower().startswith("maison"):
                continue
            a = r["adresse_ban"]
            if a not in best or str(r.get("date_etablissement_dpe", "")) > str(best[a].get("date_etablissement_dpe", "")):
                best[a] = r
        return best

    best = choisir(maison) or (choisir(False) if maison else {})
    out = []
    for a, r in best.items():
        s = float(r["surface_habitable_logement"])
        e = (r.get("etiquette_dpe") or "").upper()
        out.append({"adresse": a, "surface": s, "classe": e, "annee": r.get("annee_construction"),
                    "date": str(r.get("date_etablissement_dpe", ""))[:10],
                    "s_surf": 1 - borne(abs(s - hab) / tol),
                    "s_classe": 0.5 if not classe or not e else (1.0 if e == classe else 0.0)})
    out.sort(key=lambda d: -(0.6 * d["s_surf"] + 0.4 * d["s_classe"]))
    return out[:15]


def geocoder(adresse):
    try:
        r = requests.get(GEOCODE_URL, params={"q": adresse, "limit": 1}, timeout=30)
        r.raise_for_status()
        lon, lat = r.json()["features"][0]["geometry"]["coordinates"][:2]
        return lat, lon
    except Exception:
        return None


def situer_dpe(dpe, parcelles, args):
    """Place chaque adresse DPE sur la carte et retrouve sa parcelle cadastrale."""
    for d in dpe:
        d["pos"] = geocoder(d["adresse"])
    if any(d["pos"] for d in dpe):
        formes, index = [], []
        for i, ft in enumerate(parcelles["features"]):
            try:
                formes.append(shape(ft["geometry"]))
                index.append(i)
            except Exception:
                continue
        arbre = STRtree(formes)
        for d in dpe:
            if not d["pos"]:
                continue
            pt = Point(d["pos"][1], d["pos"][0])
            idx = arbre.query(pt, predicate="intersects")
            if not len(idx):
                idx = arbre.query_nearest(pt, max_distance=0.0003)
            if len(idx):
                pr = parcelles["features"][index[int(idx[0])]]["properties"]
                d["parcelle"] = pr.get("id")
                d["terrain"] = int(pr.get("contenance") or 0)
                d["geom"] = mapping(formes[int(idx[0])])
    tol = max(args.tolerance, 0.25)
    for d in dpe:
        s_t = 1 - borne(abs(d["terrain"] - args.terrain) / (args.terrain * tol)) if d.get("terrain") else 0.5
        d["score"] = round(100 * (0.45 * d["s_surf"] + 0.2 * d["s_classe"] + 0.35 * s_t))
        d["s_dpe"] = round(100 * (0.6 * d["s_surf"] + 0.4 * d["s_classe"]))
    dpe.sort(key=lambda d: -d["score"])
    return dpe


def liens_html(lat, lon):
    return " · ".join(f'<a href="{u}" target="_blank" rel="noopener">{k}</a>' for k, u in liens(lat, lon).items())


def detail_dpe(d):
    t = f"DPE {d['classe'] or '?'} · {int(d['surface'])} m²"
    if d.get("annee"):
        t += f" · construit {d['annee']}"
    return t


def fusionner(res, dpe, args):
    """Une seule liste : parcelles du cadastre + adresses DPE, avec scores cadastre, DPE et global."""
    par, libres = {}, []
    for d in sorted(dpe, key=lambda d: -d["s_dpe"]):
        pid = d.get("parcelle")
        if not pid:
            libres.append(d)
        elif pid not in par:
            par[pid] = d  # la meilleure adresse DPE de la parcelle
    deja = set()
    for r in res:
        d = par.get(r["parcelle"])
        r.update(dpe=d["adresse"] if d else "", s_dpe=d["s_dpe"] if d else None,
                 dpe_pos=d.get("pos") if d else None, dpe_detail=detail_dpe(d) if d else "",
                 dpe_classe=d["classe"] if d else "", dpe_annee=d.get("annee") if d else None)
        deja.add(r["parcelle"])
    for pid, d in par.items():  # parcelle repérée par le DPE mais au terrain différent de l'annonce
        if pid in deja:
            continue
        pos = d.get("pos")
        res.append({"parcelle": pid, "terrain_m2": d.get("terrain") or 0, "emprise_principale_m2": None,
                    "nb_batiments": 0, "forme_rectangle_pct": None,
                    "s_terrain": score_terrain(d["terrain"], args.terrain, args.tolerance) if d.get("terrain") else None,
                    "s_emprise": None, "s_forme": None, "geom": d.get("geom"),
                    "lat": round(pos[0], 6) if pos else None, "lon": round(pos[1], 6) if pos else None,
                    "dpe": d["adresse"], "s_dpe": d["s_dpe"], "dpe_pos": pos, "dpe_detail": detail_dpe(d),
                    "dpe_classe": d["classe"], "dpe_annee": d.get("annee")})
    for d in libres:  # adresse DPE sans parcelle retrouvée : on la garde dans la liste
        pos = d.get("pos")
        res.append({"parcelle": "", "terrain_m2": 0, "emprise_principale_m2": None, "nb_batiments": 0,
                    "forme_rectangle_pct": None, "geom": None,
                    "lat": round(pos[0], 6) if pos else None, "lon": round(pos[1], 6) if pos else None,
                    "dpe": d["adresse"], "s_dpe": d["s_dpe"], "dpe_pos": pos, "dpe_detail": detail_dpe(d),
                    "dpe_classe": d["classe"], "dpe_annee": d.get("annee")})
    for r in res:
        r["s_cad"] = score_cadastre(r) if r.get("geom") else None
        r["s_glob"] = score_global(r["s_cad"], r["s_dpe"])
    res.sort(key=lambda r: -r["s_glob"])
    return res


def liens_ligne(r):
    if r.get("lat") is not None:
        return liens_html(r["lat"], r["lon"])
    q = quote(r["dpe"])
    return f'<a href="https://www.google.com/maps/search/?api=1&query={q}" target="_blank" rel="noopener">Google Maps</a>'


def ecrire(res, nom, args, dpe=None):
    OUT.mkdir(exist_ok=True)
    v = lambda x: "" if x is None else x
    with open(OUT / "candidats.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rang", "indice_global", "score_cadastre", "score_dpe", "parcelle", "adresse_dpe", "detail_dpe",
                    "terrain_m2", "emprise_principale_m2", "forme_rectangle_pct", "lat", "lon"])
        for i, r in enumerate(res, 1):
            w.writerow([i, r["s_glob"], v(r["s_cad"]), v(r["s_dpe"]), r["parcelle"], r.get("dpe", ""),
                        r.get("dpe_detail", ""), r["terrain_m2"] or "", v(r["emprise_principale_m2"]),
                        v(r["forme_rectangle_pct"]), v(r["lat"]), v(r["lon"])])
    feats = []
    for r in res:
        a = liens_ligne(r)
        titre = r["parcelle"] or r["dpe"]
        info = (f"<b>{html.escape(titre)}</b><br>Global {r['s_glob']} · cadastre {v(r['s_cad']) or '–'} · "
                f"DPE {v(r['s_dpe']) if r['s_dpe'] is not None else '–'}")
        if r["terrain_m2"]:
            info += f"<br>Terrain {r['terrain_m2']} m²"
            if r["emprise_principale_m2"]:
                info += f" · bâtiment {r['emprise_principale_m2']} m²"
        if r.get("dpe"):
            info += f"<br>✔ {html.escape(r['dpe'])}<br>{html.escape(r['dpe_detail'])}"
        info += f"<br>{a}"
        pos = r.get("dpe_pos")
        feats.append({"type": "Feature", "geometry": r.get("geom"),
                      "properties": {"popup": info, "s_glob": r["s_glob"], "s_cad": r["s_cad"], "s_dpe": r["s_dpe"],
                                     "terrain": r["terrain_m2"] or None, "emprise": r["emprise_principale_m2"],
                                     "nom": r["parcelle"], "dpe": r.get("dpe", ""), "liens": a,
                                     "pos": [pos[0], pos[1]] if pos else None,
                                     "forme": r["forme_rectangle_pct"], "classe": r.get("dpe_classe", ""),
                                     "annee": r.get("dpe_annee")}})
    data = json.dumps({"type": "FeatureCollection", "features": feats}).replace("</", "<\\/")
    page = PAGE.replace("__DATA__", data) \
               .replace("__TITRE__", html.escape(f"{nom} – {len(res)} candidats")) \
               .replace("__CRIT__", html.escape(f"Terrain ≈ {args.terrain:.0f} m² (±{args.tolerance*100:.0f} %), "
                                                f"habitable ≈ {args.habitable:.0f} m²"))
    (OUT / "candidats.html").write_text(page, encoding="utf-8")
    return OUT / "candidats.html"


PAGE = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITRE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{font:15px system-ui,sans-serif;margin:0}#map{height:55vh}main{padding:12px 16px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}tbody tr{cursor:pointer}
.ctl{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:8px 0}.ctl label{display:flex;gap:6px;align-items:center}
.leg span{display:inline-block;width:12px;height:12px;border-radius:2px;margin:0 4px 0 10px;vertical-align:middle}
.flt{margin:8px 0}.mute{color:#666;font-size:.85rem}#err{color:#b91c1c;font-weight:600}</style>
</head><body><div id="map"></div><main><h2>__TITRE__</h2><p>__CRIT__</p><div id="err"></div>
<div class="ctl">
<label>Méthode <select id="mode"><option value="glob">Indice global (cadastre + DPE)</option>
<option value="cad">Cadastre seul</option><option value="dpe">DPE seul</option></select></label>
<label>Score minimum <b id="minv">0</b> <input type="range" id="min" min="0" max="100" value="0"></label>
<label><input type="checkbox" id="showdpe" checked> Points des adresses DPE</label></div>
<details class="flt"><summary><b>Filtres avancés</b></summary><div class="ctl">
<label><input type="checkbox" id="f_dpe"> Avec adresse DPE seulement</label>
<label>Classe énergie <select id="f_cl"><option value="">toutes</option><option>A</option><option>B</option><option>C</option><option>D</option><option>E</option><option>F</option><option>G</option></select></label>
<label>Construit à partir de <input type="number" id="f_an" placeholder="ex. 2010" style="width:90px"></label>
<label>Terrain (m²) <input type="number" id="f_t0" placeholder="min" style="width:80px"> – <input type="number" id="f_t1" placeholder="max" style="width:80px"></label>
<label>Bâtiment (m²) <input type="number" id="f_b0" placeholder="min" style="width:80px"> – <input type="number" id="f_b1" placeholder="max" style="width:80px"></label>
<label><input type="checkbox" id="f_lu"> Bâtiment en L / U (pas un simple rectangle)</label>
<button id="f_rz" type="button">Réinitialiser</button></div>
<p class="mute">Les lignes dont l’information est inconnue (année, bâtiment…) ne sont pas écartées par le filtre correspondant.</p></details>
<p class="leg">Couleur :<span style="background:#dc2626"></span>85 et +<span style="background:#f97316"></span>70-84<span style="background:#eab308"></span>50-69<span style="background:#9ca3af"></span>moins de 50</p>
<p class="mute" id="stat"></p>
<div style="overflow-x:auto"><table><thead><tr><th>#</th><th>Global</th><th>Cadastre</th><th>DPE</th><th>Parcelle / adresse DPE</th><th>Terrain m²</th><th>Bâtiment m²</th><th>Liens</th></tr></thead>
<tbody id="rows"></tbody></table></div>
<p class="mute">Une seule liste. Indice global = 55 % cadastre (terrain, emprise du bâtiment, forme) + 45 % DPE (surface habitable et classe énergie d’une adresse tombant sur la parcelle) ;
si un seul des deux existe, il compte pour 75 %. Touche une ligne pour la voir sur la carte. Adresses issues de la base publique ADEME : à utiliser pour repérer le bien
avant de contacter l’agence ou le vendeur, dans le respect de la vie privée des occupants.</p></main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var data=__DATA__,refs=[],map=null,lay=null,dl=null;
function $(i){return document.getElementById(i)}
function err(e){$('err').textContent='Problème d’affichage de la carte : '+(e&&e.message?e.message:e)}
try{
  var ortho=L.tileLayer('https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg',{maxZoom:19,attribution:'© IGN'});
  var osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'});
  map=L.map('map',{center:[46.6,2.4],zoom:6,layers:[ortho]});
  L.control.layers({'Photo aérienne IGN':ortho,'Plan OSM':osm}).addTo(map);
  lay=L.featureGroup().addTo(map);dl=L.featureGroup().addTo(map);
}catch(e){map=null;err(e)}
function sc(p){var m=$('mode').value;return m=='cad'?p.s_cad:(m=='dpe'?p.s_dpe:p.s_glob)}
function col(v){return v>=85?'#dc2626':(v>=70?'#f97316':(v>=50?'#eab308':'#9ca3af'))}
function cell(tr,t){var td=document.createElement('td');td.textContent=t;tr.appendChild(td);return td}
function num(id){var v=$(id).value;return v===''?null:+v}
function ok(p){
  if($('f_dpe').checked&&!p.dpe)return false;
  var cl=$('f_cl').value;if(cl&&p.classe!==cl)return false;
  var an=num('f_an');if(an!==null&&p.annee&&+p.annee<an)return false;
  var t0=num('f_t0'),t1=num('f_t1');
  if(p.terrain){if(t0!==null&&p.terrain<t0)return false;if(t1!==null&&p.terrain>t1)return false}
  var b0=num('f_b0'),b1=num('f_b1');
  if(p.emprise){if(b0!==null&&p.emprise<b0)return false;if(b1!==null&&p.emprise>b1)return false}
  if($('f_lu').checked&&!(p.forme!==null&&p.forme!==undefined&&p.forme<=85))return false;
  return true}
function items(){var mn=+$('min').value;
  return data.features.filter(function(f){var v=sc(f.properties);return v!==null&&v!==undefined&&v>=mn&&ok(f.properties)})
    .sort(function(a,b){return sc(b.properties)-sc(a.properties)})}
function renderTable(its){
  var tb=$('rows');tb.innerHTML='';
  its.forEach(function(f,i){
    var p=f.properties,tr=document.createElement('tr');
    cell(tr,i+1);cell(tr,p.s_glob).style.fontWeight='700';cell(tr,p.s_cad===null?'–':p.s_cad);cell(tr,p.s_dpe===null?'–':p.s_dpe);
    cell(tr,(p.nom?p.nom+' ':'')+(p.dpe?'· '+p.dpe:''));cell(tr,p.terrain||'–');cell(tr,p.emprise||'–');cell(tr,'').innerHTML=p.liens;
    tr.onclick=function(){var r=refs[i];if(!r||!map)return;
      if(r.getBounds){map.fitBounds(r.getBounds().pad(.6))}else{map.setView(r.getLatLng(),18)}r.openPopup()};
    tb.appendChild(tr);
  });
  if(!its.length){var tr=document.createElement('tr');cell(tr,'Aucun résultat à ce niveau : baisse le score minimum ou change de méthode.').colSpan=8;tb.appendChild(tr)}
}
function flat(c,out){if(typeof c[0]=='number'){out.push(c)}else{c.forEach(function(x){flat(x,out)})}}
function rings(g){if(!g)return[];if(g.type=='Polygon')return g.coordinates;
  if(g.type=='MultiPolygon'){var r=[];g.coordinates.forEach(function(p){p.forEach(function(x){r.push(x)})});return r}return[]}
function plan(its){ /* secours si la bibliothèque de carte ne se charge pas : contours seuls */
  var m=$('map'),pts=[];its.forEach(function(f){if(f.geometry)flat(f.geometry.coordinates,pts)});
  if(!pts.length){m.textContent='Aucun contour à afficher.';return}
  var x0=1e9,x1=-1e9,y0=1e9,y1=-1e9;pts.forEach(function(c){x0=Math.min(x0,c[0]);x1=Math.max(x1,c[0]);y0=Math.min(y0,c[1]);y1=Math.max(y1,c[1])});
  var k=Math.cos((y0+y1)/2*Math.PI/180),s=560/Math.max((x1-x0)*k,(y1-y0),1e-9),out='<svg viewBox="-10 -10 580 580" style="width:100%;height:100%;background:#f3f4f6">';
  its.forEach(function(f){var c=col(sc(f.properties));rings(f.geometry).forEach(function(r){
    out+='<path d="'+r.map(function(q,i){return(i?'L':'M')+((q[0]-x0)*k*s).toFixed(1)+','+((y1-q[1])*s).toFixed(1)}).join('')+'Z" fill="'+c+'" fill-opacity=".35" stroke="'+c+'" stroke-width="2"/>'})});
  m.innerHTML=out+'</svg>';
}
function renderMap(its,refit){
  if(typeof L==='undefined'||!map){plan(its);return}
  lay.clearLayers();dl.clearLayers();refs=[];
  its.forEach(function(f,i){
    var p=f.properties,c=col(sc(p)),ref=null;
    if(f.geometry){L.geoJSON(f,{style:function(){return{color:c,weight:3,fillColor:c,fillOpacity:.2}},
      onEachFeature:function(ft,l){l.bindPopup(p.popup);ref=l}}).addTo(lay)}
    if(p.pos){var mk=L.circleMarker(p.pos,{radius:8,color:'#1d4ed8',weight:3,fillColor:c,fillOpacity:.9}).bindPopup(p.popup).addTo(dl);if(!ref)ref=mk}
    refs[i]=ref;
  });
  if(refit){var b=L.latLngBounds([]);  /* on cadre les 8 meilleurs résultats, pas toute la commune */
    its.slice(0,8).forEach(function(f){if(f.geometry){b.extend(L.geoJSON(f).getBounds())}if(f.properties.pos){b.extend(f.properties.pos)}});
    if(b.isValid()){map.fitBounds(b.pad(.2),{maxZoom:18})}}
}
function render(refit){
  var its=items();$('minv').textContent=$('min').value;
  $('stat').textContent=its.length+' résultat(s) sur '+data.features.length;
  try{renderTable(its)}catch(e){err(e)}
  try{renderMap(its,refit)}catch(e){err(e)}
}
$('mode').onchange=function(){render(true)};$('min').oninput=function(){render(false)};
['f_dpe','f_cl','f_an','f_t0','f_t1','f_b0','f_b1','f_lu'].forEach(function(i){$(i).oninput=function(){render(true)};$(i).onchange=function(){render(true)}});
$('f_rz').onclick=function(){$('f_dpe').checked=false;$('f_lu').checked=false;$('f_cl').value='';['f_an','f_t0','f_t1','f_b0','f_b1'].forEach(function(i){$(i).value=''});render(true)};
$('showdpe').onchange=function(){if(!map)return;if(this.checked){map.addLayer(dl)}else{map.removeLayer(dl)}};
render(true);
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
    m = re.search(r"(?i:classe\s*(?:énergie|energie|dpe)?|dpe|énergie)\D{0,20}\b([A-G])\b", texte)
    if m:
        res["classe"] = m.group(1)
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
    dpe = []
    if getattr(args, "utiliser_dpe", True):
        try:
            dpe = situer_dpe(chercher_dpe(args, insee, nom, log), parcelles, args)
            log(f"{len(dpe)} adresse(s) trouvée(s) via les DPE.")
        except Exception as e:
            log(f"DPE indisponible ({e}) : seuls les résultats du cadastre sont affichés.")
    res = fusionner(res, dpe, args)
    if not res:
        raise RuntimeError("Aucun candidat. Élargis la tolérance ou baisse la taille minimale du bâtiment.")
    res = res[:getattr(args, "max_resultats", 60)]
    page = ecrire(res, nom, args, dpe)
    log(f"{len(res)} parcelles et {len(dpe)} adresses DPE retenues.")
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
    ap.add_argument("--classe", default="", help="classe énergie de l'annonce (A à G)")
    ap.add_argument("--hab-tol", dest="hab_tol", type=float, default=0.08, help="écart toléré sur la surface habitable (0.08 = ±8 %%)")
    ap.add_argument("--sans-dpe", dest="utiliser_dpe", action="store_false", help="ne pas interroger les DPE")
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
