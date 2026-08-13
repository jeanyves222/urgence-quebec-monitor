#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Capture des relevés d'urgence du MSSS — comité « Mes soins restent ICI ».

Une seule exécution fait deux choses :

  1. CSV HORAIRE — téléchargé à chaque passage. Le fichier est écrasé toutes
     les heures à la source ; ce qui n'est pas capté est perdu. Classé sous
     son heure d'extraction, jamais réécrit.

  2. RELEVÉ QUOTIDIEN (PDF) — téléchargé une fois par jour seulement, après
     sa régénération de 11 h 45. S'il a déjà été capté aujourd'hui, on passe.

Le point important est la VALIDATION. Les deux fichiers peuvent être servis
dans un état dégradé :
  - le relevé quotidien a déjà été diffusé sous forme de gabarit, avec ses
    champs de fusion non résolus (&jour. &mois. &annee.) et aucune donnée ;
  - le CSV peut être tronqué ou vide pendant sa régénération.
Un fichier qui ne passe pas la validation n'est PAS archivé : on ressort en
code 0, le passage suivant réessaiera. Archiver un gabarit vide pendant des
semaines en croyant accumuler des données serait le pire des scénarios.

Usage :
    python3 scripts/capture.py            # capture normale
    python3 scripts/capture.py --dry-run  # télécharge et valide sans écrire
"""

import argparse
import csv
import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extraire_releve_quotidien import extraire as extraire_quotidien  # noqa: E402

# ── Sources ────────────────────────────────────────────────────────────────
BASE = "https://msss.gouv.qc.ca/professionnels/statistiques/documents/urgences/"
URL_CSV_HORAIRE = BASE + "Releve_horaire_urgences_7jours.csv"
URL_PDF_QUOTIDIEN = BASE + "Rap_Quotid_SituatUrgence1.pdf"
URL_PDF_HORAIRE = BASE + "Rap_horaire_SituatUrgence1.pdf"

# Le site refuse les requêtes sans en-tête crédible. Les pages HTML ont une
# détection de robots ; le répertoire /documents/ n'en a pas, mais autant
# rester poli et identifiable.
# ATTENTION : les en-tetes HTTP doivent etre encodables en latin-1. Un tiret
# cadratin ou une lettre accentuee ici fait planter requests avec une
# UnicodeEncodeError avant meme le telechargement. Garder ces valeurs en ASCII.
ENTETES = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 "
        "(collecte citoyenne - comite Mes soins restent ICI)"
    ),
    "Accept": "*/*",
    "Accept-Language": "fr-CA,fr;q=0.9",
}

DELAI = 60          # secondes
RACINE = Path(__file__).resolve().parent.parent
DOSSIER_HORAIRE = RACINE / "data" / "horaire"
DOSSIER_QUOTIDIEN = RACINE / "data" / "quotidien"

# ── Seuils de validation ───────────────────────────────────────────────────
# Le CSV portait 120 installations le 12 août 2026. On refuse un fichier
# nettement plus court : c'est le signe d'une régénération en cours.
MIN_LIGNES_CSV = 90
# Le relevé quotidien portait 98 installations + 15 régions au test du 7 août.
MIN_INSTALLATIONS_PDF = 70
# Champs de fusion d'un gabarit non renseigné.
MARQUEURS_GABARIT = ("&jour.", "&mois.", "&annee.", "&jr_m_v")

FUSEAU_MONTREAL = timezone(timedelta(hours=-4))  # EDT ; EST en hiver, sans effet ici


def maintenant_montreal():
    """Heure locale approximative. Sert uniquement à dater les captures et à
    savoir si le relevé quotidien du jour est déjà pris — une heure d'écart
    en période de changement d'heure n'a aucune conséquence ici."""
    return datetime.now(timezone.utc).astimezone(FUSEAU_MONTREAL)


def journal(niveau, message):
    horodatage = maintenant_montreal().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{horodatage}] {niveau:12s} {message}", flush=True)


def telecharger(url):
    """Retourne (contenu_binaire, None) ou (None, message_d_erreur)."""
    try:
        r = requests.get(url, headers=ENTETES, timeout=DELAI)
    except requests.RequestException as err:
        return None, f"échec réseau : {err}"
    if r.status_code != 200:
        return None, f"code HTTP {r.status_code}"
    if not r.content:
        return None, "réponse vide"
    return r.content, None


# ══════════════════════════════════════════════════════════════════════════
# 1. CSV HORAIRE
# ══════════════════════════════════════════════════════════════════════════

def valider_csv(contenu):
    """Retourne (infos, None) si le fichier est exploitable, sinon (None, motif).

    infos contient l'heure d'extraction et l'horodatage de mise à jour, qui
    servent à nommer le fichier et à ne pas archiver deux fois la même heure.
    """
    try:
        texte = contenu.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            texte = contenu.decode("latin-1")
        except Exception as err:
            return None, f"encodage illisible : {err}"

    lignes = [l for l in texte.splitlines() if l.strip()]
    if len(lignes) < 2:
        return None, "fichier sans données"

    lecteur = csv.DictReader(io.StringIO(texte))
    # Les en-têtes contiennent des tabulations et des espaces de remplissage.
    if not lecteur.fieldnames:
        return None, "en-tête absent"
    champs = {re.sub(r"\s+", "", c or ""): c for c in lecteur.fieldnames}

    def champ(fragment):
        for propre, brut in champs.items():
            if fragment in propre:
                return brut
        return None

    col_installation = champ("Nom_installation")
    col_heure = champ("Heure_de_l")
    col_maj = champ("Mise_a_jour")
    if not col_installation or not col_heure:
        return None, f"colonnes attendues absentes ({lecteur.fieldnames})"

    rangs = [r for r in lecteur if (r.get(col_installation) or "").strip()]
    if len(rangs) < MIN_LIGNES_CSV:
        return None, f"seulement {len(rangs)} installation(s), minimum {MIN_LIGNES_CSV}"

    heures = {(r.get(col_heure) or "").strip() for r in rangs}
    heures.discard("")
    if not heures:
        return None, "aucune heure d'extraction"

    maj = ""
    if col_maj:
        valeurs = {(r.get(col_maj) or "").strip() for r in rangs}
        valeurs.discard("")
        maj = sorted(valeurs)[-1] if valeurs else ""

    return {
        "installations": len(rangs),
        "heure_extraction": sorted(heures)[-1],
        "mise_a_jour": maj,
    }, None


def cle_horaire(infos):
    """Nom de fichier fondé sur l'horodatage DE LA SOURCE, jamais sur l'heure
    d'exécution. Si le workflow tourne en retard ou deux fois, on retombe sur
    le même nom et on n'archive pas de doublon."""
    maj = infos.get("mise_a_jour") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", maj)
    if m:
        a, mo, j, h, mi = m.groups()
        return a, mo, j, f"{h}{mi}"
    # Repli : la date d'exécution avec l'heure d'extraction déclarée.
    n = maintenant_montreal()
    h = re.sub(r"\D", "", infos.get("heure_extraction", ""))[:4] or n.strftime("%H%M")
    return n.strftime("%Y"), n.strftime("%m"), n.strftime("%d"), h


def capturer_csv_horaire(dry_run):
    contenu, err = telecharger(URL_CSV_HORAIRE)
    if err:
        journal("HORAIRE-ECHEC", f"téléchargement impossible — {err}")
        return False

    infos, err = valider_csv(contenu)
    if err:
        journal("HORAIRE-REJET", f"fichier non conforme — {err}")
        return False

    annee, mois, jour, heure = cle_horaire(infos)
    dossier = DOSSIER_HORAIRE / annee / mois
    chemin = dossier / f"releve_horaire_{annee}-{mois}-{jour}_{heure}.csv"

    if chemin.exists():
        journal("HORAIRE-DEJA", f"{chemin.name} déjà archivé — rien à faire")
        return False

    journal("HORAIRE-OK",
            f"{infos['installations']} installations, extraction {infos['heure_extraction']}, "
            f"mise à jour {infos['mise_a_jour'] or 'non déclarée'}")

    if dry_run:
        journal("DRY-RUN", f"aurait écrit {chemin.relative_to(RACINE)}")
        return False

    dossier.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)  # octets bruts : l'archive reste fidèle à la source
    journal("HORAIRE-ECRIT", str(chemin.relative_to(RACINE)))
    return True


# ══════════════════════════════════════════════════════════════════════════
# 2. RELEVÉ QUOTIDIEN (PDF)
# ══════════════════════════════════════════════════════════════════════════

MOIS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


def valider_pdf_quotidien(chemin_temporaire):
    """Ouvre le PDF, refuse le gabarit non renseigné, retourne (date, nb_installations)."""
    import pdfplumber

    with pdfplumber.open(chemin_temporaire) as pdf:
        if len(pdf.pages) < 5:
            return None, f"seulement {len(pdf.pages)} page(s)"
        texte = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])

    for marqueur in MARQUEURS_GABARIT:
        if marqueur in texte:
            return None, ("gabarit non renseigné — champ de fusion "
                          f"« {marqueur} » présent, la génération de 11 h 45 a échoué")

    m = re.search(r"Mise à jour\s*:?\s*(\d{1,2})\s+([A-Za-zéûôàî]+)\s+(\d{4})", texte)
    if not m:
        return None, "date de mise à jour introuvable"
    jour, mois_txt, annee = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if mois_txt not in MOIS_FR:
        return None, f"mois non reconnu : {mois_txt}"
    date_rapport = datetime(annee, MOIS_FR[mois_txt], jour).date()

    lignes, _ = extraire_quotidien(str(chemin_temporaire))
    installations = {l["installation"] for l in lignes if l["niveau"] == "installation"}
    if len(installations) < MIN_INSTALLATIONS_PDF:
        return None, (f"seulement {len(installations)} installation(s) extraites, "
                      f"minimum {MIN_INSTALLATIONS_PDF}")

    return (date_rapport, lignes, installations), None


def capturer_pdf_quotidien(dry_run):
    aujourdhui = maintenant_montreal().date()
    dossier = DOSSIER_QUOTIDIEN / f"{aujourdhui:%Y}"
    chemin_pdf = dossier / f"rap_quotid_{aujourdhui:%Y-%m-%d}.pdf"
    chemin_csv = dossier / f"rap_quotid_{aujourdhui:%Y-%m-%d}.csv"

    if chemin_pdf.exists():
        journal("QUOTID-DEJA", f"{chemin_pdf.name} déjà archivé aujourd'hui")
        return False

    # Le relevé est régénéré à 11 h 45. Inutile d'insister avant.
    if maintenant_montreal().hour < 12:
        journal("QUOTID-ATTENTE", "avant 12 h, le relevé du jour n'est pas encore régénéré")
        return False

    contenu, err = telecharger(URL_PDF_QUOTIDIEN)
    if err:
        journal("QUOTID-ECHEC", f"téléchargement impossible — {err}")
        return False

    temporaire = RACINE / ".rap_quotid_temporaire.pdf"
    temporaire.write_bytes(contenu)
    try:
        resultat, err = valider_pdf_quotidien(temporaire)
        if err:
            journal("QUOTID-REJET", f"{err} — non archivé, reprise au prochain passage")
            return False

        date_rapport, lignes, installations = resultat
        if date_rapport != aujourdhui:
            journal("QUOTID-ATTENTE",
                    f"le relevé en ligne porte encore la date du {date_rapport} — "
                    f"pas encore régénéré pour aujourd'hui")
            return False

        journal("QUOTID-OK",
                f"relevé du {date_rapport}, {len(installations)} installations, "
                f"{len(lignes)} lignes extraites")

        if dry_run:
            journal("DRY-RUN", f"aurait écrit {chemin_pdf.name} et {chemin_csv.name}")
            return False

        dossier.mkdir(parents=True, exist_ok=True)
        chemin_pdf.write_bytes(contenu)

        champs = ["date_rapport", "date", "region_code", "region", "installation",
                  "niveau", "indicateur", "valeur", "moy_5sem_an_courant",
                  "moy_5sem_an_precedent", "civieres_fonctionnelles", "source"]
        with open(chemin_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=champs)
            w.writeheader()
            w.writerows(lignes)

        journal("QUOTID-ECRIT", f"{chemin_pdf.name} + {chemin_csv.name}")
        return True
    finally:
        temporaire.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="télécharge et valide sans rien écrire")
    ap.add_argument("--horaire-seulement", action="store_true")
    ap.add_argument("--quotidien-seulement", action="store_true")
    args = ap.parse_args()

    ecritures = 0
    if not args.quotidien_seulement:
        ecritures += int(capturer_csv_horaire(args.dry_run))
    if not args.horaire_seulement:
        ecritures += int(capturer_pdf_quotidien(args.dry_run))

    # Code 0 même sans écriture : un rejet de validation ou un fichier déjà
    # présent sont des situations normales, pas des échecs du workflow.
    journal("BILAN", f"{ecritures} fichier(s) archivé(s)")
    # Transmis au workflow pour qu'il ne commite que s'il y a du neuf.
    sortie = os.environ.get("GITHUB_OUTPUT")
    if sortie:
        with open(sortie, "a", encoding="utf-8") as f:
            f.write(f"ecritures={ecritures}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
