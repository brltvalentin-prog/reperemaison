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
    """Cadastre + DPE quand une adresse DPE tombe sur la parcelle ; sinon cadastre seul, un peu pénalisé."""
    return round(0.55 * s_cad + 0.45 * s_dpe) if s_dpe is not None else round(0.75 * s_cad)


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


def fusionner(res, dpe, args):
    """Rapproche parcelles du cadastre et adresses DPE, calcule les trois scores (cadastre, DPE, global)."""
    par = {}
    for d in dpe:
        if d.get("parcelle") and (d["parcelle"] not in par or d["s_dpe"] > par[d["parcelle"]]["s_dpe"]):
            par[d["parcelle"]] = d
    deja = set()
    for r in res:
        d = par.get(r["parcelle"])
        r["dpe"] = d["adresse"] if d else ""
        r["s_dpe"] = d["s_dpe"] if d else None
        deja.add(r["parcelle"])
    for pid, d in par.items():  # parcelles repérées par le DPE seulement (terrain différent de l'annonce)
        if pid in deja or not d.get("geom") or not d.get("pos"):
            continue
        res.append({"parcelle": pid, "terrain_m2": d.get("terrain") or 0, "emprise_principale_m2": None,
                    "nb_batiments": 0, "forme_rectangle_pct": None,
                    "s_terrain": score_terrain(d["terrain"], args.terrain, args.tolerance) if d.get("terrain") else None,
                    "s_emprise": None, "s_forme": None, "lat": round(d["pos"][0], 6), "lon": round(d["pos"][1], 6),
                    "geom": d["geom"], "dpe": d["adresse"], "s_dpe": d["s_dpe"]})
    for r in res:
        r["s_cad"] = score_cadastre(r)
        r["s_glob"] = score_global(r["s_cad"], r["s_dpe"])
    res.sort(key=lambda r: -r["s_glob"])
    return res


def ecrire(res, nom, args, dpe=None):
    dpe = dpe or []
    OUT.mkdir(exist_ok=True)
    with open(OUT / "candidats.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rang", "parcelle", "terrain_m2", "emprise_principale_m2", "nb_batiments",
                    "forme_rectangle_pct", "score_global", "score_cadastre", "score_dpe", "lat", "lon",
                    "satellite", "street_view", "adresse_dpe"])
        for i, r in enumerate(res, 1):
            l = liens(r["lat"], r["lon"])
            w.writerow([i, r["parcelle"], r["terrain_m2"], r["emprise_principale_m2"] or "", r["nb_batiments"],
                        r["forme_rectangle_pct"] or "", r["s_glob"], r["s_cad"],
                        "" if r["s_dpe"] is None else r["s_dpe"], r["lat"], r["lon"],
                        l["Satellite"], l["Street View"], r.get("dpe", "")])
    if dpe:
        with open(OUT / "candidats_dpe.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["rang", "adresse", "classe", "surface_m2", "annee_construction", "date_dpe",
                        "parcelle", "terrain_m2", "score"])
            for i, d in enumerate(dpe, 1):
                w.writerow([i, d["adresse"], d["classe"], d["surface"], d.get("annee") or "", d["date"],
                            d.get("parcelle", ""), d.get("terrain", ""), d["score"]])
    feats = []
    for r in res:
        a = liens_html(r["lat"], r["lon"])
        dpe_txt = "–" if r["s_dpe"] is None else r["s_dpe"]
        extra = f"<br>✔ DPE : {html.escape(r['dpe'])}" if r.get("dpe") else ""
        info = (f"<b>{html.escape(r['parcelle'])}</b><br>Global {r['s_glob']} · cadastre {r['s_cad']} · DPE {dpe_txt}<br>"
                f"Terrain {r['terrain_m2']} m²"
                + (f" · bâtiment {r['emprise_principale_m2']} m²" if r["emprise_principale_m2"] else "")
                + f"{extra}<br>{a}")
        feats.append({"type": "Feature", "geometry": r["geom"],
                      "properties": {"popup": info, "s_glob": r["s_glob"], "s_cad": r["s_cad"], "s_dpe": r["s_dpe"],
                                     "terrain": r["terrain_m2"], "emprise": r["emprise_principale_m2"],
                                     "dpe": r.get("dpe", ""), "liens": a}})
    pts, dl = [], []
    for i, d in enumerate(dpe, 1):
        detail = f"DPE {d['classe'] or '?'} · {int(d['surface'])} m²"
        if d.get("terrain"):
            detail += f" · terrain {d['terrain']} m²"
        if d.get("annee"):
            detail += f" · construit {d['annee']}"
        a = liens_html(*d["pos"]) if d.get("pos") else ""
        dl.append(f"<tr><td>{i}</td><td>{html.escape(d['adresse'])}</td><td>{detail}</td>"
                  f"<td><b>{d['score']}</b></td><td>{a}</td></tr>")
        if d.get("pos"):
            pts.append({"lat": d["pos"][0], "lon": d["pos"][1],
                        "popup": f"<b>DPE #{i}</b><br>{html.escape(d['adresse'])}<br>{detail}<br>{a}"})
    bloc = ""
    if dl:
        bloc = ("<h3>Adresses trouvées via les diagnostics énergétiques (DPE)</h3>"
                "<p>Cercles bleus sur la carte. Base publique ADEME : à utiliser pour repérer le bien avant de "
                "contacter l’agence ou le vendeur, dans le respect de la vie privée des occupants.</p>"
                "<div style='overflow-x:auto'><table><tr><th>#</th><th>Adresse</th><th>Détail</th><th>Score</th><th>Liens</th></tr>"
                + "\n".join(dl) + "</table></div><h3>Parcelles candidates (cadastre)</h3>")
    data = json.dumps({"type": "FeatureCollection", "features": feats}).replace("</", "<\\/")
    page = PAGE.replace("__DATA__", data) \
               .replace("__PTS__", json.dumps(pts).replace("</", "<\\/")).replace("__DPE__", bloc) \
               .replace("__TITRE__", html.escape(f"{nom} – {len(dpe)} adresse(s) DPE, {len(res)} parcelles")) \
               .replace("__CRIT__", html.escape(f"Terrain ≈ {args.terrain:.0f} m² (±{args.tolerance*100:.0f} %), "
                                                f"habitable ≈ {args.habitable:.0f} m²"))
    (OUT / "candidats.html").write_text(page, encoding="utf-8")
    return OUT / "candidats.html"


PAGE = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITRE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{font:15px system-ui,sans-serif;margin:0}#map{height:55vh}main{padding:12px 16px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}
.ctl{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:8px 0}.ctl label{display:flex;gap:6px;align-items:center}
.leg span{display:inline-block;width:12px;height:12px;border-radius:2px;margin:0 4px 0 10px;vertical-align:middle}
.mute{color:#666;font-size:.85rem}</style>
</head><body><div id="map"></div><main><h2>__TITRE__</h2><p>__CRIT__</p>
<div class="ctl">
<label>Méthode <select id="mode"><option value="glob">Score global (cadastre + DPE)</option>
<option value="cad">Cadastre seul</option><option value="dpe">DPE seul</option></select></label>
<label>Score minimum <b id="minv">0</b> <input type="range" id="min" min="0" max="100" value="0"></label>
<label><input type="checkbox" id="showdpe" checked> Adresses DPE (cercles bleus)</label></div>
<p class="leg">Contour des parcelles :<span style="background:#dc2626"></span>85 et +<span style="background:#f97316"></span>70-84<span style="background:#eab308"></span>50-69<span style="background:#9ca3af"></span>moins de 50</p>
__DPE__
<div style="overflow-x:auto"><table><thead><tr><th>#</th><th>Global</th><th>Cadastre</th><th>DPE</th><th>Terrain m²</th><th>Bâtiment m²</th><th>Adresse DPE</th><th>Liens</th></tr></thead>
<tbody id="rows"></tbody></table></div>
<p class="mute">Score cadastre = terrain, emprise du bâtiment et forme. Score DPE = surface habitable et classe énergie d’une adresse tombant sur la parcelle.
Score global = 55 % cadastre + 45 % DPE ; sans adresse DPE sur la parcelle, cadastre × 0,75. Le tableau est trié selon la méthode choisie.</p></main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var data=__DATA__,pts=__PTS__,premier=true;
var ortho=L.tileLayer('https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg',{maxZoom:19,attribution:'© IGN'});
var osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'});
var map=L.map('map',{layers:[ortho]});L.control.layers({'Photo aérienne IGN':ortho,'Plan OSM':osm}).addTo(map);
var lay=L.layerGroup().addTo(map),dl=L.layerGroup().addTo(map);
pts.forEach(function(p){L.circleMarker([p.lat,p.lon],{radius:9,color:'#1d4ed8',weight:3,fillOpacity:.5}).bindPopup(p.popup).addTo(dl)});
function $(i){return document.getElementById(i)}
function sc(p){var m=$('mode').value;return m=='cad'?p.s_cad:(m=='dpe'?p.s_dpe:p.s_glob)}
function col(v){return v>=85?'#dc2626':(v>=70?'#f97316':(v>=50?'#eab308':'#9ca3af'))}
function cell(tr,t){var td=document.createElement('td');td.textContent=t;tr.appendChild(td);return td}
function render(){
  lay.clearLayers();var tb=$('rows');tb.innerHTML='';
  var mn=+$('min').value;$('minv').textContent=mn;
  var items=data.features.filter(function(f){var v=sc(f.properties);return v!==null&&v!==undefined&&v>=mn})
    .sort(function(a,b){return sc(b.properties)-sc(a.properties)});
  items.forEach(function(f,i){
    var p=f.properties,c=col(sc(p));
    L.geoJSON(f,{style:function(){return{color:c,weight:3,fillColor:c,fillOpacity:.2}},
      onEachFeature:function(ft,l){l.bindPopup(p.popup)}}).addTo(lay);
    var tr=document.createElement('tr');
    cell(tr,i+1);cell(tr,p.s_glob);cell(tr,p.s_cad);cell(tr,p.s_dpe===null?'–':p.s_dpe);cell(tr,p.terrain);
    cell(tr,p.emprise===null?'–':p.emprise);cell(tr,p.dpe||'');cell(tr,'').innerHTML=p.liens;
    tb.appendChild(tr);
  });
  if(!items.length){var tr=document.createElement('tr');cell(tr,'Aucune parcelle à ce niveau : baisse le score minimum ou change de méthode.').colSpan=8;tb.appendChild(tr)}
  if(premier){premier=false;var b=L.geoJSON(data).getBounds();pts.forEach(function(p){b.extend([p.lat,p.lon])});
    if(b.isValid()){map.fitBounds(b.pad(.1))}else{map.setView([46.6,2.4],6)}}
}
$('mode').onchange=render;$('min').oninput=render;
$('showdpe').onchange=function(){if(this.checked){map.addLayer(dl)}else{map.removeLayer(dl)}};
render();
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
    if not res and not dpe:
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
