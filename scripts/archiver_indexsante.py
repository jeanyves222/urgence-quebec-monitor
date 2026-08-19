#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archivage de la page brute d'Index Santé — comité « Mes soins restent ICI ».

POURQUOI ARCHIVER LA PAGE BRUTE
-------------------------------
Les fichiers de Données Québec ne portent que les civières, les séjours de plus
de 24 et 48 heures et le nombre de personnes présentes. Ils ne portent NI les
durées moyennes de séjour, NI le nombre de personnes en attente de voir un
médecin — or ce sont ces deux indicateurs qui portent l'argument de première
ligne du comité. Seul Index Santé les publie.

Depuis le changement de structure du 2 août 2026, la page nationale porte les
deux DMS directement dans la ligne de chaque installation. Archiver cette SEULE
page suffit donc à tout reconstituer : inutile de récupérer les 97 fiches.

Le bénéfice principal n'est pas la redondance, c'est la RÉANALYSE
RÉTROACTIVE. Quand la logique d'extraction change, ou qu'un défaut est
découvert après coup, une archive brute permet de refaire l'analyse sur toute
la série. Les incidents passés — corruption des colonnes E et F en juillet,
sommaire national servi vide en août — auraient tous été récupérables.

DEUX BANDEAUX, PAS UN
---------------------
La page porte « Dernière mise à jour : ... » ET « Dernière mise à jour complète
des données et des taux : ... ». Le second n'est capté nulle part ailleurs. Il
pourrait expliquer les pages dégradées : rafraîchissement partiel effectué
pendant que la mise à jour complète accuse du retard. Les deux sont relevés ici
et inscrits dans l'index, ce qui permettra de les comparer sans jamais avoir à
décompresser une archive.

Usage :
    python3 scripts/archiver_indexsante.py
    python3 scripts/archiver_indexsante.py --dry-run
"""

import argparse
import csv
import gzip
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

URL_INDEXSANTE = "https://www.indexsante.ca/urgences/"

# ATTENTION : les en-tetes HTTP doivent etre encodables en latin-1. Un tiret
# cadratin ou une lettre accentuee ici fait planter requests avec une
# UnicodeEncodeError avant meme le telechargement. Garder ces valeurs en ASCII.
ENTETES = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5",
}

DELAI = 60
RACINE = Path(__file__).resolve().parent.parent
DOSSIER = RACINE / "data" / "indexsante"
INDEX = DOSSIER / "index.csv"

# La page faisait 233 479 octets au test du 18 août 2026. On refuse tout ce qui
# est nettement plus court : c'est le signe d'une page d'erreur ou tronquée.
TAILLE_MINIMALE = 100_000
ANCRE_BLOC_GLOBAL = "Situation dans l'ensemble des urgences"

MOIS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

FUSEAU_MONTREAL = timezone(timedelta(hours=-4))  # EDT ; EST en hiver, sans effet ici

CHAMPS_INDEX = [
    "capture_utc", "maj_source", "maj_complete", "retard_min",
    "octets", "octets_compresses", "empreinte", "fichier",
]


def maintenant_montreal():
    return datetime.now(timezone.utc).astimezone(FUSEAU_MONTREAL)


def journal(niveau, message):
    horodatage = maintenant_montreal().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{horodatage}] {niveau:14s} {message}", flush=True)


def decoder_entites(texte):
    """Index Santé écrit les lettres accentuées en code HTML — « ao&ucirc;t »
    plutôt que « août ». Sans ce décodage, aucune recherche de nom de mois ne
    peut aboutir."""
    remplacements = {
        "&agrave;": "à", "&acirc;": "â", "&eacute;": "é", "&egrave;": "è",
        "&ecirc;": "ê", "&euml;": "ë", "&icirc;": "î", "&iuml;": "ï",
        "&ocirc;": "ô", "&ouml;": "ö", "&ucirc;": "û", "&ugrave;": "ù",
        "&uuml;": "ü", "&ccedil;": "ç", "&nbsp;": " ", "&amp;": "&",
    }
    for code, lettre in remplacements.items():
        texte = texte.replace(code, lettre)
    return re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), texte)


def texte_brut(html):
    """Retire les balises et écrase les espaces, comme le fait la feuille 1."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", decoder_entites(html)))


def deux(n):
    return f"{n:02d}"


def extraire_maj_simple(texte):
    """« Dernière mise à jour : 18 août 2026 à 13:45 » -> « 2026-08-18 13:45 ».
    Le motif exige un chiffre juste après le deux-points, ce qui écarte
    naturellement le second bandeau, dont le libellé se poursuit par
    « complète des données et des taux »."""
    m = re.search(
        r"Derni[eè]re\s+mise\s+[aà]\s+jour\s*:?\s*(\d{1,2})\s+([A-Za-zéèêûôàïîç]+)"
        r"\s+(\d{4})\s+[aà]\s+(\d{1,2})\s*[:h]\s*(\d{2})", texte, re.I)
    if not m:
        return ""
    mois = MOIS_FR.get(m.group(2).lower())
    if not mois:
        return ""
    return (f"{m.group(3)}-{deux(mois)}-{deux(int(m.group(1)))} "
            f"{deux(int(m.group(4)))}:{m.group(5)}")


def extraire_maj_complete(texte):
    """Second bandeau, dont le format exact reste à découvrir. On relève donc
    ce qui suit le libellé sans présumer de sa forme, en s'arrêtant à la
    première rupture nette. Normalisé si c'est une date reconnaissable, gardé
    tel quel sinon — mieux vaut une valeur brute qu'une valeur perdue."""
    m = re.search(r"Derni[eè]re\s+mise\s+[aà]\s+jour\s+compl[eè]te[^:]*:\s*([^<]{0,80})",
                  texte, re.I)
    if not m:
        return ""
    valeur = m.group(1).strip()

    d = re.match(r"(\d{1,2})\s+([A-Za-zéèêûôàïîç]+)\s+(\d{4})"
                 r"(?:\s+[aà]\s+(\d{1,2})\s*[:h]\s*(\d{2}))?", valeur, re.I)
    if d:
        mois = MOIS_FR.get(d.group(2).lower())
        if mois:
            jour = f"{d.group(3)}-{deux(mois)}-{deux(int(d.group(1)))}"
            if d.group(4):
                return f"{jour} {deux(int(d.group(4)))}:{d.group(5)}"
            return jour
    return re.sub(r"\s+", " ", valeur)[:60]


def retard_minutes(maj_source):
    """Écart entre l'horodatage déclaré par la page et l'instant présent.
    C'est la mesure qui permettra de dire si la source se dégrade."""
    try:
        d = datetime.strptime(maj_source, "%Y-%m-%d %H:%M").replace(tzinfo=FUSEAU_MONTREAL)
    except (ValueError, TypeError):
        return ""
    return int((maintenant_montreal() - d).total_seconds() // 60)


def deja_indexe(nom_fichier):
    """L'index fait foi : si la capture y figure, la page n'a pas changé depuis
    le dernier passage et il n'y a rien à archiver."""
    if not INDEX.exists():
        return False
    with open(INDEX, newline="", encoding="utf-8-sig") as f:
        for ligne in csv.DictReader(f):
            if ligne.get("fichier") == nom_fichier:
                return True
    return False


def ajouter_a_index(entree):
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    nouveau = not INDEX.exists()
    with open(INDEX, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CHAMPS_INDEX)
        if nouveau:
            w.writeheader()
        w.writerow(entree)


def main():
    ap = argparse.ArgumentParser(description="Archivage de la page d'Index Santé")
    ap.add_argument("--dry-run", action="store_true",
                    help="télécharge et valide sans rien écrire")
    args = ap.parse_args()

    try:
        r = requests.get(URL_INDEXSANTE, headers=ENTETES, timeout=DELAI)
    except requests.RequestException as err:
        journal("ECHEC", f"téléchargement impossible — {err}")
        return 0

    if r.status_code != 200:
        journal("ECHEC", f"code HTTP {r.status_code}")
        return 0

    html = r.content
    if len(html) < TAILLE_MINIMALE:
        journal("REJET", f"page trop courte — {len(html)} octets, minimum {TAILLE_MINIMALE}")
        return 0

    texte = texte_brut(html.decode("utf-8", errors="replace"))
    if ANCRE_BLOC_GLOBAL not in texte:
        journal("REJET", f"ancre « {ANCRE_BLOC_GLOBAL} » absente — page inattendue")
        return 0

    maj_source = extraire_maj_simple(texte)
    maj_complete = extraire_maj_complete(texte)
    empreinte = hashlib.sha256(html).hexdigest()[:12]

    # Nommage par l'horodatage DE LA SOURCE : une exécution en retard ou
    # rejouée retombe sur le même nom et n'archive pas de doublon. Sans
    # horodatage exploitable, on se rabat sur l'empreinte du contenu.
    if maj_source:
        jour, heure = maj_source.split(" ")
        annee, mois = jour.split("-")[0], jour.split("-")[1]
        nom = f"indexsante_{jour}_{heure.replace(':', '')}.html.gz"
    else:
        n = maintenant_montreal()
        annee, mois = n.strftime("%Y"), n.strftime("%m")
        nom = f"indexsante_{n:%Y-%m-%d}_{empreinte}.html.gz"
        journal("SANS-HORODATAGE",
                "bandeau de mise à jour illisible — nommage par empreinte")

    chemin = DOSSIER / annee / mois / nom
    retard = retard_minutes(maj_source)

    journal("PAGE-OK",
            f"{len(html)} octets, mise à jour {maj_source or 'non captée'}, "
            f"complète {maj_complete or 'non captée'}, "
            f"retard {retard if retard != '' else '?'} min")

    if deja_indexe(nom) or chemin.exists():
        journal("DEJA", f"{nom} déjà archivé — la source n'a pas changé")
        return 0

    if args.dry_run:
        journal("DRY-RUN", f"aurait écrit {chemin.relative_to(RACINE)}")
        return 0

    chemin.parent.mkdir(parents=True, exist_ok=True)
    comprime = gzip.compress(html, compresslevel=9)
    chemin.write_bytes(comprime)

    ajouter_a_index({
        "capture_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maj_source": maj_source,
        "maj_complete": maj_complete,
        "retard_min": retard,
        "octets": len(html),
        "octets_compresses": len(comprime),
        "empreinte": empreinte,
        "fichier": nom,
    })

    journal("ARCHIVE", f"{chemin.relative_to(RACINE)} ({len(comprime)} octets compressés)")

    sortie = os.environ.get("GITHUB_OUTPUT")
    if sortie:
        with open(sortie, "a", encoding="utf-8") as f:
            f.write("ecritures=1\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
