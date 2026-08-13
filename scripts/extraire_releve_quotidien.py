#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extraction du « Relevé quotidien de la situation à l'urgence » (MSSS / CPU)
vers un CSV en format long, une ligne par installation × date × indicateur.

Usage :
    python3 extraire_releve_quotidien.py Rap_Quotid_SituatUrgence1.pdf [sortie.csv]

Dépendance : pip install pdfplumber
"""

import csv
import re
import sys
import unicodedata
from datetime import date

import pdfplumber

# ── Structure d'une ligne du tableau ────────────────────────────────
# 1 nombre  : civières fonctionnelles (colonne « Civ »)
# 4 blocs de 9 : 7 valeurs quotidiennes + moy. 5 sem. An. en cours + An. préc.
#                (civière, +24 h, +48 h, visites totales la veille)
# 1 bloc de 7  : taux d'occupation quotidien (sans moyennes)
# Total attendu : 1 + 9*4 + 7 = 44 nombres
BLOCS = ["patients_civiere", "civiere_plus_24h", "civiere_plus_48h", "visites_totales"]
N_ATTENDU = 44

MOIS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

RE_NOMBRE = re.compile(r"-?\d+|N/D|N/A")   # N/D : donnée manquante au relevé
RE_REGION = re.compile(r"\((\d{2})\)\s+(.+)")
RE_DATE_RAPPORT = re.compile(r"(\d{1,2})\s+([A-Za-zéûôà]+)\s+(\d{4})")
RE_JOUR = re.compile(r"(\d{2})/(\d{2})")

IGNORER = (
    "Notes:", "Note:", "Les cinq dernières", "Source", "MSSS,", "En collaboration",
    "Mise à jour", "Relevé quotidien", "Patients sur civière", "Moyenne 5",
    "Sept derniers jours", "semaines", "An.", "Installation", "RSS", "cours",
    "S D L M M J V", "cours cours",
)


def sans_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def date_du_rapport(texte):
    m = RE_DATE_RAPPORT.search(texte)
    if not m:
        return None
    jour, mois, annee = m.group(1), sans_accents(m.group(2).lower()), m.group(3)
    if mois not in MOIS and mois not in [sans_accents(k) for k in MOIS]:
        return None
    num = MOIS.get(mois) or MOIS.get(m.group(2).lower())
    return date(int(annee), num, int(jour))


def dates_colonnes(texte, ref):
    """Reconstruit les 7 dates depuis l'entête jj/mm, en s'appuyant sur l'année
    du rapport (gère le passage d'année en fin décembre)."""
    vus, sortie = set(), []
    for j, m in RE_JOUR.findall(texte):
        cle = (int(m), int(j))
        if cle in vus:
            continue
        vus.add(cle)
        annee = ref.year - 1 if (int(m) == 12 and ref.month == 1) else ref.year
        try:
            sortie.append(date(annee, int(m), int(j)))
        except ValueError:
            pass
        if len(sortie) == 7:
            break
    return sortie


def lignes_de_donnees(texte):
    """Rend (code_region, nom, [44 nombres]) pour chaque ligne du tableau.
    Gère les noms d'installation qui débordent sur la ligne précédente
    ou suivante (ex. « Centre hospitalier régional du / Grand Portage »)."""
    lignes = [l.rstrip() for l in texte.split("\n")]
    resultats, prefixe, consommee = [], "", -1

    for i, ligne in enumerate(lignes):
        if i == consommee:
            continue
        nu = ligne.strip()
        if not nu or any(nu.startswith(p) for p in IGNORER):
            prefixe = ""
            continue

        # Le code de région « (01) » ne doit pas être compté comme une donnée
        code = None
        m_code = re.match(r"\s*\((\d{2})\)\s*", ligne)
        if m_code:
            code = m_code.group(1)
            ligne = ligne[m_code.end():]

        nombres = RE_NOMBRE.findall(ligne)
        if len(nombres) < N_ATTENDU:
            if not nombres and len(nu) > 3:
                prefixe = (prefixe + " " + nu).strip()
            continue

        pos = RE_NOMBRE.search(ligne).start()
        propre = ligne[:pos].strip()
        nom = (prefixe + " " + propre).strip()

        # Le nom ne déborde sur la ligne suivante que s'il ne tient pas
        # sur la ligne de données elle-même.
        if not propre and i + 1 < len(lignes):
            suite = lignes[i + 1].strip()
            if (suite and not RE_NOMBRE.search(suite) and len(suite) < 60
                    and not any(suite.startswith(p) for p in IGNORER)):
                nom = (nom + " " + suite).strip()
                consommee = i + 1

        nom = re.sub(r"\s+", " ", nom)
        if nom:
            resultats.append((code, nom, [None if x in ("N/D", "N/A") else int(x)
                                          for x in nombres[:N_ATTENDU]]))
        prefixe = ""

    return resultats


def eclater(nom, vals, region_code, region_nom, jours, ref, source, page_province=False):
    """Transforme les 44 nombres en lignes longues."""
    lignes = []
    civ_fonct = vals[0] if vals[0] is not None else ""
    est_total = nom.lower().startswith("total")
    commun = dict(
        date_rapport=ref.isoformat(),
        region_code=region_code,
        region=region_nom,
        installation=nom,
        niveau=("total" if est_total else
                ("region" if page_province else "installation")),
        civieres_fonctionnelles=civ_fonct,
        source=source,
    )

    i = 1
    for bloc in BLOCS:
        quotidien, moy_courante, moy_prec = vals[i:i + 7], vals[i + 7], vals[i + 8]
        i += 9
        for j, v in zip(jours, quotidien):
            lignes.append({**commun, "date": j.isoformat(), "indicateur": bloc,
                           "valeur": "" if v is None else v,
                           "moy_5sem_an_courant": "", "moy_5sem_an_precedent": ""})
        lignes.append({**commun, "date": "", "indicateur": bloc + "_moy_5sem",
                       "valeur": "",
                       "moy_5sem_an_courant": "" if moy_courante is None else moy_courante,
                       "moy_5sem_an_precedent": "" if moy_prec is None else moy_prec})

    for j, v in zip(jours, vals[i:i + 7]):
        lignes.append({**commun, "date": j.isoformat(), "indicateur": "taux_occupation_pct",
                       "valeur": "" if v is None else v,
                       "moy_5sem_an_courant": "", "moy_5sem_an_precedent": ""})
    return lignes


def extraire(chemin_pdf):
    sorties, ref, jours = [], None, []
    with pdfplumber.open(chemin_pdf) as pdf:
        for page in pdf.pages:
            texte = page.extract_text() or ""
            if "Relevé quotidien" not in texte:
                continue
            if ref is None:
                ref = date_du_rapport(texte)
            if ref is None:
                continue

            trouvees = dates_colonnes(texte, ref)
            if len(trouvees) == 7:
                jours = trouvees
            elif not jours:
                print(f"  ! page ignorée : {len(trouvees)} dates détectées", file=sys.stderr)
                continue
            # sinon : page de continuation, on garde les dates de la page précédente

            m = RE_REGION.search(texte[:400])
            if m:
                code, nom_reg = m.group(1), m.group(2).strip()
            else:
                code, nom_reg = None, "Ensemble du Québec"

            for code_ligne, nom, vals in lignes_de_donnees(texte):
                sorties += eclater(nom, vals, code_ligne or code, nom_reg, jours, ref,
                                   "MSSS-CPU relevé quotidien", page_province=(code is None))
    return sorties, ref


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    chemin = sys.argv[1]
    lignes, ref = extraire(chemin)
    if not lignes:
        print("Aucune donnée extraite — le format du PDF a peut-être changé.", file=sys.stderr)
        sys.exit(2)

    sortie = sys.argv[2] if len(sys.argv) > 2 else f"releve_quotidien_{ref.isoformat()}.csv"
    champs = ["date_rapport", "date", "region_code", "region", "installation", "niveau",
              "indicateur", "valeur", "moy_5sem_an_courant", "moy_5sem_an_precedent",
              "civieres_fonctionnelles", "source"]
    with open(sortie, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs)
        w.writeheader()
        w.writerows(lignes)

    inst = {l["installation"] for l in lignes if l["niveau"] == "installation"}
    print(f"{len(lignes)} lignes · {len(inst)} installations · rapport du {ref} → {sortie}")


if __name__ == "__main__":
    main()
