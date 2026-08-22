#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Capture des relevés d'urgence — comité « Mes soins restent ICI ».

POURQUOI CETTE VERSION
----------------------
Le 13 août 2026, msss.gouv.qc.ca a refusé toutes les requêtes venant de
GitHub Actions : HTTP 403, page nginx nue, refus identique avec des en-têtes
de navigateur complets et avec des en-têtes de client générique. Un refus
insensible aux en-têtes est un blocage d'adresse IP, pas un filtre : aucun
ajustement ne le contournera depuis un centre de données.

Les mêmes données sont diffusées sur Données Québec, le portail de données
ouvertes du gouvernement, sous licence CC-BY 4.0 — un site conçu pour l'accès
automatisé. C'est là que ce script puise désormais.

CE QUI EST CAPTÉ
----------------
  1. FICHIER HORAIRE — patients sur civière, +24 h, +48 h, civières
     fonctionnelles, par installation, avec le numéro de permis. Écrasé toutes
     les heures à la source : ce qui n'est pas capté est perdu.
  2. FICHIER HORAIRE AVEC PERSONNES PRÉSENTES — même maille, colonne de plus.
  3. FICHIER CUMULATIF (BDCU) — par installation, par période financière, avec
     le portrait des quatre dernières années financières. Il ne change qu'aux
     périodes financières : on le vérifie chaque jour et on l'archive
     seulement s'il a bougé. C'est le seul historique d'avant la collecte.

CE QUI N'EST PAS CAPTÉ ICI
--------------------------
Le relevé quotidien en PDF (visites totales, taux d'occupation, moyennes des
cinq dernières semaines avec la colonne « année précédente ») n'existe que
sur msss.gouv.qc.ca, donc hors d'atteinte depuis GitHub. Mettre
CAPTURER_PDF_QUOTIDIEN à True pour le récupérer en exécutant ce script depuis
un poste sur adresse résidentielle, où le 403 ne se produit pas.

VALIDATION
----------
Un fichier qui ne passe pas la validation n'est PAS archivé ; le passage
suivant réessaie. Archiver un fichier vide ou tronqué pendant des semaines en
croyant accumuler des données serait le pire des scénarios.

Usage :
    python3 scripts/capture.py
    python3 scripts/capture.py --dry-run          # valide sans rien écrire
    python3 scripts/capture.py --avec-pdf         # force le PDF quotidien
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── Données Québec (CKAN) ──────────────────────────────────────────────────
CKAN = "https://www.donneesquebec.ca/recherche"

# Identifiants des ressources, relevés sur les fiches de Données Québec.
RES_HORAIRE = "a9272cc9-8234-40d1-9806-9f6b4c75c20d"
RES_HORAIRE_NBPERS = "b256f87f-40ec-4c79-bdba-a23e9c50e741"
# Le fichier cumulatif est retrouvé par le nom de son jeu : son identifiant de
# ressource n'a pas été relevé, et le demander au portail évite de le figer.
JEU_CUMULATIF = "fichier-cumulatif-des-donnees-des-urgences"

# Relevé quotidien en PDF — inaccessible depuis un centre de données.
URL_PDF_QUOTIDIEN = ("https://msss.gouv.qc.ca/professionnels/statistiques/"
                     "documents/urgences/Rap_Quotid_SituatUrgence1.pdf")
CAPTURER_PDF_QUOTIDIEN = False

# ATTENTION : les en-tetes HTTP doivent etre encodables en latin-1. Un tiret
# cadratin ou une lettre accentuee ici fait planter requests avec une
# UnicodeEncodeError avant meme le telechargement. Garder ces valeurs en ASCII.
ENTETES = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/json,*/*;q=0.8",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5",
}

DELAI = 90          # secondes
RACINE = Path(__file__).resolve().parent.parent
DOSSIER_HORAIRE = RACINE / "data" / "horaire"
DOSSIER_NBPERS = RACINE / "data" / "horaire_nbpers"
DOSSIER_CUMULATIF = RACINE / "data" / "cumulatif"
DOSSIER_QUOTIDIEN = RACINE / "data" / "quotidien"

# Le fichier horaire portait 120 installations le 12 août 2026. On refuse un
# fichier nettement plus court : signe d'une régénération en cours.
MIN_LIGNES = 90
MIN_INSTALLATIONS_PDF = 70
MARQUEURS_GABARIT = ("&jour.", "&mois.", "&annee.", "&jr_m_v")

# Fuseau NOMMÉ, jamais un décalage fixe. Le Québec passe à l'heure normale
# le 1er novembre 2026 : un décalage figé à -4 aurait alors faussé de
# soixante minutes le retard calculé, c'est-à-dire précisément la mesure
# de fiabilité de la source. « America/Montreal » bascule tout seul.
FUSEAU_MONTREAL = ZoneInfo("America/Montreal")


def maintenant_montreal():
    return datetime.now(timezone.utc).astimezone(FUSEAU_MONTREAL)


def journal(niveau, message):
    horodatage = maintenant_montreal().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{horodatage}] {niveau:16s} {message}", flush=True)


def obtenir(url, session=None, **kw):
    """GET simple. Retourne (reponse, None) ou (None, motif lisible)."""
    client = session or requests
    try:
        r = client.get(url, headers=ENTETES, timeout=DELAI, **kw)
    except requests.RequestException as err:
        return None, f"échec réseau : {err}"
    if r.status_code != 200:
        apercu = (r.text or "")[:120].replace("\n", " ").strip()
        return None, f"code HTTP {r.status_code} — {apercu or 'corps vide'}"
    if not r.content:
        return None, "réponse vide"
    return r, None


# ══════════════════════════════════════════════════════════════════════════
# Récupération d'une ressource CKAN
# ══════════════════════════════════════════════════════════════════════════

def resource_id_du_jeu(nom_jeu, motif_format="CSV"):
    """Demande au portail les ressources d'un jeu et rend l'identifiant de la
    première au format voulu. Évite de figer un identifiant qui pourrait
    changer si le diffuseur remplace la ressource."""
    url = f"{CKAN}/api/3/action/package_show?id={nom_jeu}"
    r, err = obtenir(url)
    if err:
        return None, f"fiche du jeu « {nom_jeu} » illisible — {err}"
    try:
        paquet = r.json()["result"]
    except (ValueError, KeyError) as err:
        return None, f"réponse inattendue du portail : {err}"
    for res in paquet.get("resources", []):
        if str(res.get("format", "")).upper() == motif_format.upper():
            return res.get("id"), None
    return None, f"aucune ressource {motif_format} dans « {nom_jeu} »"


def telecharger_ressource(resource_id):
    """Récupère une ressource du datastore de Données Québec, en CSV.

    Deux voies, essayées dans l'ordre :
      1. le vidage direct du datastore, qui rend le CSV complet d'un coup ;
      2. l'interrogation paginée du datastore, reconstituée en CSV.
    La seconde sert de filet si le vidage est désactivé ou trop lourd.
    """
    with requests.Session() as session:
        url_vidage = f"{CKAN}/datastore/dump/{resource_id}?format=csv&bom=true"
        r, err = obtenir(url_vidage, session=session)
        if r is not None:
            return r.content, None
        journal("VIDAGE-ECHEC", f"{resource_id[:8]} — {err} ; essai par pagination")

        lignes, colonnes, decalage = [], None, 0
        while True:
            url = (f"{CKAN}/api/3/action/datastore_search"
                   f"?resource_id={resource_id}&limit=10000&offset={decalage}")
            r, err = obtenir(url, session=session)
            if err:
                return None, f"pagination interrompue à {decalage} — {err}"
            try:
                res = r.json()["result"]
            except (ValueError, KeyError) as err:
                return None, f"réponse inattendue à {decalage} — {err}"

            enregistrements = res.get("records", [])
            if colonnes is None:
                # On écarte la colonne technique _id ajoutée par le portail.
                colonnes = [c["id"] for c in res.get("fields", []) if c["id"] != "_id"]
            lignes.extend(enregistrements)
            if len(enregistrements) < 10000:
                break
            decalage += len(enregistrements)
            if decalage > 200000:      # garde-fou : jamais atteint en pratique
                break

        if not lignes or not colonnes:
            return None, "datastore vide"

        tampon = io.StringIO()
        w = csv.DictWriter(tampon, fieldnames=colonnes, extrasaction="ignore")
        w.writeheader()
        w.writerows(lignes)
        return tampon.getvalue().encode("utf-8-sig"), None


# ══════════════════════════════════════════════════════════════════════════
# Validation et nommage
# ══════════════════════════════════════════════════════════════════════════

def lire_csv(contenu):
    """Décode et rend (colonnes, rangées) ou lève ValueError."""
    for encodage in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            texte = contenu.decode(encodage)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("encodage illisible")

    lecteur = csv.DictReader(io.StringIO(texte))
    if not lecteur.fieldnames:
        raise ValueError("en-tête absent")
    return lecteur.fieldnames, list(lecteur)


def colonne_contenant(colonnes, fragment):
    """Retrouve une colonne malgré les tabulations, espaces et accents que la
    source insère dans ses en-têtes."""
    cible = fragment.lower()
    for c in colonnes:
        if cible in re.sub(r"\s+", "", (c or "")).lower():
            return c
    return None


def valider(contenu, minimum=MIN_LIGNES):
    """Rend (infos, None) si le fichier est exploitable, sinon (None, motif)."""
    try:
        colonnes, rangs = lire_csv(contenu)
    except ValueError as err:
        return None, str(err)

    col_inst = colonne_contenant(colonnes, "nom_installation") or \
        colonne_contenant(colonnes, "installation")
    if not col_inst:
        return None, f"colonne d'installation absente ({colonnes[:6]})"

    rangs = [r for r in rangs if (r.get(col_inst) or "").strip()]
    if len(rangs) < minimum:
        return None, f"seulement {len(rangs)} ligne(s), minimum {minimum}"

    def derniere_valeur(fragment):
        col = colonne_contenant(colonnes, fragment)
        if not col:
            return ""
        valeurs = {(r.get(col) or "").strip() for r in rangs}
        valeurs.discard("")
        return sorted(valeurs)[-1] if valeurs else ""

    return {
        "lignes": len(rangs),
        "installations": len({(r.get(col_inst) or "").strip() for r in rangs}),
        "mise_a_jour": derniere_valeur("mise_a_jour"),
        "heure_extraction": derniere_valeur("heure_de_l"),
        "empreinte": hashlib.sha256(contenu).hexdigest()[:12],
    }, None


def horodatage_fichier(infos):
    """Nom fondé sur l'horodatage DE LA SOURCE, jamais sur l'heure d'exécution.
    Une exécution en retard, rejouée ou déclenchée à la main retombe sur le
    même nom et n'archive pas de doublon. À défaut d'horodatage dans les
    données, on se rabat sur l'empreinte du contenu — deux fichiers identiques
    donnent alors le même nom."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})",
                 infos.get("mise_a_jour") or "")
    if m:
        a, mo, j, h, mi = m.groups()
        return a, mo, f"{a}-{mo}-{j}_{h}{mi}"
    n = maintenant_montreal()
    return n.strftime("%Y"), n.strftime("%m"), f"{n:%Y-%m-%d}_{infos['empreinte']}"


# ══════════════════════════════════════════════════════════════════════════
# Les trois captures
# ══════════════════════════════════════════════════════════════════════════

def capturer_ressource(etiquette, resource_id, dossier, prefixe, dry_run,
                       minimum=MIN_LIGNES):
    contenu, err = telecharger_ressource(resource_id)
    if err:
        journal(f"{etiquette}-ECHEC", err)
        return False

    infos, err = valider(contenu, minimum)
    if err:
        journal(f"{etiquette}-REJET", f"fichier non conforme — {err}")
        return False

    annee, mois, cle = horodatage_fichier(infos)
    chemin = dossier / annee / mois / f"{prefixe}_{cle}.csv"

    if chemin.exists():
        journal(f"{etiquette}-DEJA", f"{chemin.name} déjà archivé")
        return False

    journal(f"{etiquette}-OK",
            f"{infos['lignes']} lignes, {infos['installations']} installations, "
            f"mise à jour {infos['mise_a_jour'] or 'non déclarée'}")

    if dry_run:
        journal("DRY-RUN", f"aurait écrit {chemin.relative_to(RACINE)}")
        return False

    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    journal(f"{etiquette}-ECRIT", str(chemin.relative_to(RACINE)))
    return True


def capturer_cumulatif(dry_run):
    """Le fichier cumulatif ne bouge qu'aux périodes financières. On le
    vérifie chaque jour et on ne l'archive que s'il a changé — d'où le nommage
    par empreinte du contenu."""
    resource_id, err = resource_id_du_jeu(JEU_CUMULATIF)
    if err:
        journal("CUMUL-ECHEC", err)
        return False
    return capturer_ressource("CUMUL", resource_id, DOSSIER_CUMULATIF,
                              "cumulatif_urgences", dry_run, minimum=50)


MOIS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


def capturer_pdf_quotidien(dry_run):
    """Ne fonctionne que depuis une adresse résidentielle : msss.gouv.qc.ca
    refuse les centres de données."""
    from extraire_releve_quotidien import extraire as extraire_quotidien
    import pdfplumber

    aujourdhui = maintenant_montreal().date()
    dossier = DOSSIER_QUOTIDIEN / f"{aujourdhui:%Y}"
    chemin_pdf = dossier / f"rap_quotid_{aujourdhui:%Y-%m-%d}.pdf"
    chemin_csv = dossier / f"rap_quotid_{aujourdhui:%Y-%m-%d}.csv"

    if chemin_pdf.exists():
        journal("QUOTID-DEJA", f"{chemin_pdf.name} déjà archivé aujourd'hui")
        return False
    if maintenant_montreal().hour < 12:
        journal("QUOTID-ATTENTE", "avant 12 h, le relevé du jour n'est pas régénéré")
        return False

    r, err = obtenir(URL_PDF_QUOTIDIEN)
    if err:
        journal("QUOTID-ECHEC", f"{err} (attendu depuis un centre de données)")
        return False

    temporaire = RACINE / ".rap_quotid_temporaire.pdf"
    temporaire.write_bytes(r.content)
    try:
        with pdfplumber.open(temporaire) as pdf:
            if len(pdf.pages) < 5:
                journal("QUOTID-REJET", f"seulement {len(pdf.pages)} page(s)")
                return False
            texte = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])

        for marqueur in MARQUEURS_GABARIT:
            if marqueur in texte:
                journal("QUOTID-REJET",
                        f"gabarit non renseigné — champ « {marqueur} » présent")
                return False

        m = re.search(r"Mise à jour\s*:?\s*(\d{1,2})\s+([A-Za-zéûôàî]+)\s+(\d{4})", texte)
        if not m or m.group(2).lower() not in MOIS_FR:
            journal("QUOTID-REJET", "date de mise à jour introuvable")
            return False
        date_rapport = datetime(int(m.group(3)), MOIS_FR[m.group(2).lower()],
                                int(m.group(1))).date()
        if date_rapport != aujourdhui:
            journal("QUOTID-ATTENTE", f"le relevé porte encore la date du {date_rapport}")
            return False

        lignes, _ = extraire_quotidien(str(temporaire))
        installations = {l["installation"] for l in lignes if l["niveau"] == "installation"}
        if len(installations) < MIN_INSTALLATIONS_PDF:
            journal("QUOTID-REJET", f"seulement {len(installations)} installations")
            return False

        journal("QUOTID-OK", f"relevé du {date_rapport}, {len(installations)} "
                             f"installations, {len(lignes)} lignes")
        if dry_run:
            journal("DRY-RUN", f"aurait écrit {chemin_pdf.name}")
            return False

        dossier.mkdir(parents=True, exist_ok=True)
        chemin_pdf.write_bytes(r.content)
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
    ap = argparse.ArgumentParser(description="Capture des relevés d'urgence")
    ap.add_argument("--dry-run", action="store_true",
                    help="télécharge et valide sans rien écrire")
    ap.add_argument("--avec-pdf", action="store_true",
                    help="tente aussi le relevé quotidien du MSSS "
                         "(ne fonctionne que depuis une adresse résidentielle)")
    args = ap.parse_args()

    journal("DEBUT", f"source : Données Québec ({CKAN})")
    ecritures = 0

    ecritures += int(capturer_ressource(
        "HORAIRE", RES_HORAIRE, DOSSIER_HORAIRE, "releve_horaire", args.dry_run))
    ecritures += int(capturer_ressource(
        "NBPERS", RES_HORAIRE_NBPERS, DOSSIER_NBPERS, "releve_nbpers", args.dry_run))
    ecritures += int(capturer_cumulatif(args.dry_run))

    if CAPTURER_PDF_QUOTIDIEN or args.avec_pdf:
        ecritures += int(capturer_pdf_quotidien(args.dry_run))

    # Code 0 même sans écriture : un rejet de validation ou un fichier déjà
    # présent sont des situations normales, pas des échecs du workflow.
    journal("BILAN", f"{ecritures} fichier(s) archivé(s)")
    sortie = os.environ.get("GITHUB_OUTPUT")
    if sortie:
        with open(sortie, "a", encoding="utf-8") as f:
            f.write(f"ecritures={ecritures}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
