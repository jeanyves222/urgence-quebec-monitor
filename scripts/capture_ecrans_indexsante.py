"""
CAPTURE D'ÉCRAN INDEX SANTÉ — V1.0
----------------------------------------------------------------------------
But : conserver une preuve visuelle horodatée (screenshot pleine page) de la
page nationale d'Index Santé et des 26 installations de la catégorisation
officielle (PIVOT, COMPARABLE, CONTEXTE — voir comparables-fatima), en cas de
litige sur la fiabilité des sources.

Conçu pour tourner sur un runner GitHub HÉBERGÉ (ubuntu-latest), déclenché
toutes les 5 minutes dans une large plage d'heures UTC (voir le .yml). Le
script lui-même décide s'il doit RÉELLEMENT capturer : seulement aux mêmes
fenêtres que la Feuille 1, soit :05 et :55 des heures 0h, 8h et 16h, heure de
Montréal — calculé avec ZoneInfo, donc insensible au changement d'heure. Les
autres passages se terminent en une seconde, sans capture.

Anti-doublon : une fenêtre déjà captée aujourd'hui (dossier déjà présent) est
sautée. Rattrapage : une fenêtre manquée est encore captée jusqu'à
TOLERANCE_RATTRAPAGE_MIN minutes après l'heure cible, pour absorber les
retards habituels du planificateur de GitHub.

Sortie : captures/AAAA-MM-JJ/HHhMM/ — un PNG par cible (optimisé sans perte
avec Pillow) plus un CSV recapitulatif. Le .yml pousse le tout dans le dépôt.
----------------------------------------------------------------------------
"""

from playwright.sync_api import sync_playwright
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import csv
import os
import sys
import time
import traceback

from PIL import Image

VERSION_SCRIPT = "V1.0-CAPTURE-ECRANS-QUARTS"

FUSEAU_HORAIRE = ZoneInfo("America/Montreal")

BASE_DIR = Path("captures")

# Fenêtres cibles : mêmes heures et mêmes minutes que la Feuille 1.
HEURES_CIBLES = [0, 8, 16]
MINUTES_CIBLES = [5, 55]

# Une fenêtre manquée à l'heure pile est encore captée si on est dans ce délai.
TOLERANCE_RATTRAPAGE_MIN = 30

ATTENTE_APRES_CHARGEMENT_MS = 9000
TIMEOUT_NAVIGATION_MS = 90000
TIMEOUT_NETWORKIDLE_MS = 30000
MAX_TENTATIVES = 3
PAUSE_ENTRE_TENTATIVES_S = 10

ROLE_GLOBAL = "GLOBAL"
ROLE_PIVOT = "PIVOT"
ROLE_COMPARABLE = "COMPARABLE"
ROLE_CONTEXTE = "CONTEXTE"
BASSIN_VOISIN = "VOISIN"

# ----------------------------------------------------------------------------
# LES 27 CIBLES — même ordre et mêmes métadonnées (rôle, bassin) que
# LISTE_CIBLES dans le script de la Feuille 2 (V3.8.20), plus la page
# nationale en tête. Toute modification de cette liste doit rester alignée
# avec la Feuille 2 et avec comparables-fatima.
# ----------------------------------------------------------------------------
CIBLES = [
    {
        "code": "00_urgences_quebec",
        "nom": "Urgences Québec (page nationale)",
        "url": "https://www.indexsante.ca/urgences/",
        "role": ROLE_GLOBAL,
        "bassin": "",
    },
    {
        "code": "01_fatima",
        "nom": "Hôpital Notre-Dame-de-Fatima",
        "url": "https://www.indexsante.ca/hopitaux/bas-saint-laurent/11839/hopital-notre-dame-de-fatima.php",
        "role": ROLE_PIVOT,
        "bassin": "",
    },
    # --- COMPARABLE (19), ordre identique à LISTE_CIBLES de la Feuille 2 ---
    {
        "code": "02_notre_dame_du_lac",
        "nom": "Hôpital de Notre-Dame-du-Lac",
        "url": "https://www.indexsante.ca/hopitaux/bas-saint-laurent/4037/hopital-notre-dame-du-lac.php",
        "role": ROLE_COMPARABLE,
        "bassin": BASSIN_VOISIN,
    },
    {
        "code": "03_la_sarre",
        "nom": "Centre multiservices de santé et de services sociaux de La Sarre",
        "url": "https://www.indexsante.ca/hopitaux/abitibi-temiscamingue/4075/hopital-de-la-sarre.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "04_barrie_memorial",
        "nom": "Hôpital Barrie Memorial",
        "url": "https://www.indexsante.ca/hopitaux/monteregie/426/hopital-barrie-memorial.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "05_archipel",
        "nom": "Hôpital de l'Archipel",
        "url": "https://www.indexsante.ca/hopitaux/gaspesie-iles-de-la-madeleine/407/hopital-de-l-archipel.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "06_maniwaki",
        "nom": "Hôpital de Maniwaki",
        "url": "https://www.indexsante.ca/hopitaux/outaouais/385/hopital-de-maniwaki.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "07_amqui",
        "nom": "Hôpital d'Amqui",
        "url": "https://www.indexsante.ca/hopitaux/bas-saint-laurent/4036/hopital-amqui.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "08_sainte_anne_des_monts",
        "nom": "Hôpital de Sainte-Anne-des-Monts",
        "url": "https://www.indexsante.ca/hopitaux/gaspesie-iles-de-la-madeleine/406/hopital-de-sainte-anne-des-monts.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "09_gaspe",
        "nom": "Hôpital Hôtel-Dieu de Gaspé",
        "url": "https://www.indexsante.ca/hopitaux/gaspesie-iles-de-la-madeleine/405/hopital-hotel-dieu-de-gaspe.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "10_des_sources",
        "nom": "Centre multiservices de santé et de services sociaux des Sources",
        "url": "https://www.indexsante.ca/hopitaux/estrie/4059/hopital-asbestos.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "11_amos",
        "nom": "Hôpital d'Amos",
        "url": "https://www.indexsante.ca/hopitaux/abitibi-temiscamingue/4076/hopital-amos.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "12_la_baie",
        "nom": "Hôpital de La Baie",
        "url": "https://www.indexsante.ca/hopitaux/saguenay-lac-saint-jean/4039/hopital-de-la-baie.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "13_portneuf",
        "nom": "Hôpital régional de Portneuf",
        "url": "https://www.indexsante.ca/hopitaux/capitale-nationale/333/hopital-regional-de-portneuf.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "14_matane",
        "nom": "Hôpital de Matane",
        "url": "https://www.indexsante.ca/hopitaux/bas-saint-laurent/319/hopital-de-matane.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "15_rouyn_noranda",
        "nom": "Hôpital de Rouyn-Noranda",
        "url": "https://www.indexsante.ca/hopitaux/abitibi-temiscamingue/390/hopital-de-rouyn-noranda.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "16_mont_laurier",
        "nom": "Hôpital de Mont-Laurier",
        "url": "https://www.indexsante.ca/hopitaux/laurentides/418/hopital-de-mont-laurier.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "17_baie_saint_paul",
        "nom": "Hôpital de Baie-Saint-Paul",
        "url": "https://www.indexsante.ca/hopitaux/capitale-nationale/4051/hopital-baie-saint-paul.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "18_chandler",
        "nom": "Hôpital de Chandler",
        "url": "https://www.indexsante.ca/hopitaux/gaspesie-iles-de-la-madeleine/408/hopital-de-chandler.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "19_la_malbaie",
        "nom": "Hôpital de La Malbaie",
        "url": "https://www.indexsante.ca/hopitaux/capitale-nationale/4052/hopital-de-la-malbaie.php",
        "role": ROLE_COMPARABLE,
        "bassin": "",
    },
    {
        "code": "20_grand_portage",
        "nom": "Centre hospitalier régional du Grand-Portage",
        "url": "https://www.indexsante.ca/hopitaux/bas-saint-laurent/321/centre-hospitalier-regional-du-grand-portage.php",
        "role": ROLE_COMPARABLE,
        "bassin": BASSIN_VOISIN,
    },
    # --- CONTEXTE (6), ordre identique à LISTE_CIBLES de la Feuille 2 ------
    {
        "code": "21_montmagny",
        "nom": "Hôpital de Montmagny",
        "url": "https://www.indexsante.ca/hopitaux/chaudiere-appalaches/411/hopital-de-montmagny.php",
        "role": ROLE_CONTEXTE,
        "bassin": BASSIN_VOISIN,
    },
    {
        "code": "22_sept_iles",
        "nom": "Hôpital et CLSC de Sept-Îles",
        "url": "https://www.indexsante.ca/hopitaux/cote-nord/134/hopital-de-sept-iles.php",
        "role": ROLE_CONTEXTE,
        "bassin": "",
    },
    {
        "code": "23_le_royer",
        "nom": "CLSC et Hôpital Le Royer",
        "url": "https://www.indexsante.ca/hopitaux/cote-nord/4077/hopital-le-royer.php",
        "role": ROLE_CONTEXTE,
        "bassin": "",
    },
    {
        "code": "24_maria",
        "nom": "Hôpital de Maria",
        "url": "https://www.indexsante.ca/hopitaux/gaspesie-iles-de-la-madeleine/136/hopital-de-maria.php",
        "role": ROLE_CONTEXTE,
        "bassin": "",
    },
    {
        "code": "25_thetford_mines",
        "nom": "Hôpital de Thetford Mines",
        "url": "https://www.indexsante.ca/hopitaux/chaudiere-appalaches/410/hopital-de-thetford-mines.php",
        "role": ROLE_CONTEXTE,
        "bassin": "",
    },
    {
        "code": "26_saint_georges",
        "nom": "Hôpital de Saint-Georges",
        "url": "https://www.indexsante.ca/hopitaux/chaudiere-appalaches/412/hopital-de-saint-georges.php",
        "role": ROLE_CONTEXTE,
        "bassin": "",
    },
]

NB_CIBLES_ATTENDU = len(CIBLES)  # 27


def journaliser(journal_path, message):
    horodatage = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d %H:%M:%S")
    ligne = f"[{horodatage}] {message}"
    print(ligne)
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write(ligne + "\n")


def fenetre_cible_courante(maintenant):
    """
    Retourne (date_cible, heure_cible, minute_cible) pour la fenêtre la plus
    proche déjà passée ou en cours, si `maintenant` tombe dans la tolérance
    de rattrapage après une fenêtre cible (heure dans HEURES_CIBLES, minute
    dans MINUTES_CIBLES). Retourne None si aucune fenêtre ne s'applique
    maintenant.
    """
    for jour_delta in (0, -1):
        jour = (maintenant + timedelta(days=jour_delta)).date()
        for h in HEURES_CIBLES:
            for m in MINUTES_CIBLES:
                cible = datetime(jour.year, jour.month, jour.day, h, m, tzinfo=FUSEAU_HORAIRE)
                ecart_min = (maintenant - cible).total_seconds() / 60
                if 0 <= ecart_min <= TOLERANCE_RATTRAPAGE_MIN:
                    return cible
    return None


def optimiser_png_sans_perte(chemin_fichier):
    """Recompresse le PNG sans perte (mêmes pixels, fichier plus léger)."""
    try:
        with Image.open(chemin_fichier) as img:
            img.save(chemin_fichier, format="PNG", optimize=True, compress_level=9)
    except Exception as e:
        # Non bloquant : le PNG d'origine reste valide si l'optimisation échoue.
        print(f"AVERTISSEMENT — optimisation PNG a échoué pour {chemin_fichier} : {e}")


def capturer_une_cible(playwright, cible, dossier_sortie, journal_path):
    fichier = dossier_sortie / f"{cible['code']}.png"
    for tentative in range(1, MAX_TENTATIVES + 1):
        browser = None
        try:
            journaliser(journal_path, f"Ouverture : {cible['code']} - {cible['nom']} | tentative {tentative}")
            journaliser(journal_path, f"URL : {cible['url']}")
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="fr-CA",
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.goto(cible["url"], wait_until="domcontentloaded", timeout=TIMEOUT_NAVIGATION_MS)
            page.wait_for_timeout(ATTENTE_APRES_CHARGEMENT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=TIMEOUT_NETWORKIDLE_MS)
            except Exception:
                pass
            page.screenshot(path=str(fichier), full_page=True)
            context.close()
            browser.close()

            optimiser_png_sans_perte(fichier)

            journaliser(journal_path, f"OK capture : {fichier}")
            return {
                "code": cible["code"],
                "nom": cible["nom"],
                "role": cible["role"],
                "bassin": cible["bassin"],
                "url": cible["url"],
                "statut": "OK",
                "fichier_capture": str(fichier),
                "erreur": "",
            }
        except Exception as e:
            journaliser(journal_path, f"ERREUR tentative {tentative} : {cible['code']} - {cible['nom']}")
            journaliser(journal_path, str(e))
            journaliser(journal_path, f"Type d'erreur : {type(e).__name__}")
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            if tentative < MAX_TENTATIVES:
                journaliser(journal_path, f"Nouvelle tentative dans {PAUSE_ENTRE_TENTATIVES_S} secondes...")
                time.sleep(PAUSE_ENTRE_TENTATIVES_S)
            else:
                journaliser(journal_path, traceback.format_exc())
                return {
                    "code": cible["code"],
                    "nom": cible["nom"],
                    "role": cible["role"],
                    "bassin": cible["bassin"],
                    "url": cible["url"],
                    "statut": "ERREUR",
                    "fichier_capture": str(fichier),
                    "erreur": str(e),
                }


def ecrire_csv(dossier_sortie, resultats):
    csv_path = dossier_sortie / "resume_captures.csv"
    champs = ["code", "nom", "role", "bassin", "url", "statut", "fichier_capture", "erreur"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=champs)
        writer.writeheader()
        writer.writerows(resultats)
    return csv_path


def main():
    maintenant = datetime.now(FUSEAU_HORAIRE)
    forcer = os.environ.get("FORCER_CAPTURE", "false").strip().lower() == "true"

    if forcer:
        # Capture forcée à la demande (workflow_dispatch, option "forcer") :
        # sert à vérifier le mécanisme sans attendre une vraie fenêtre de
        # quart. Dossier nommé "test_force_..." pour ne jamais être confondu
        # avec une vraie lecture de quart dans l'analyse.
        nom_dossier = "test_force_" + maintenant.strftime("%Hh%M%S")
        dossier_jour = BASE_DIR / maintenant.strftime("%Y-%m-%d")
        dossier_sortie = dossier_jour / nom_dossier
    else:
        cible_fenetre = fenetre_cible_courante(maintenant)

        if cible_fenetre is None:
            # Passage hors fenêtre : comportement normal, la grande majorité
            # des déclenchements aux 5 minutes se terminent ici sans rien faire.
            print(f"[{maintenant:%Y-%m-%d %H:%M:%S}] Hors fenêtre de capture — rien à faire.")
            return

        nom_dossier = cible_fenetre.strftime("%Hh%M")
        dossier_jour = BASE_DIR / cible_fenetre.strftime("%Y-%m-%d")
        dossier_sortie = dossier_jour / nom_dossier

        if dossier_sortie.exists():
            print(f"[{maintenant:%Y-%m-%d %H:%M:%S}] Fenêtre {cible_fenetre:%Y-%m-%d %Hh%M} déjà captée — rien à faire.")
            return

    dossier_sortie.mkdir(parents=True, exist_ok=True)
    journal_path = dossier_sortie / "journal_capture.txt"

    journaliser(journal_path, f"Début capture Index Santé {VERSION_SCRIPT}" + (" (FORCÉE — test manuel)" if forcer else ""))
    if not forcer:
        journaliser(journal_path, f"Fenêtre cible : {cible_fenetre:%Y-%m-%d %Hh%M} (heure de Montréal)")
    journaliser(journal_path, f"Heure réelle de déclenchement : {maintenant:%Y-%m-%d %H:%M:%S}")
    journaliser(journal_path, f"Dossier de sortie : {dossier_sortie}")

    resultats = []
    with sync_playwright() as p:
        for cible in CIBLES:
            resultats.append(capturer_une_cible(p, cible, dossier_sortie, journal_path))

    csv_path = ecrire_csv(dossier_sortie, resultats)

    nb_ok = sum(1 for r in resultats if r["statut"] == "OK")
    nb_erreurs = sum(1 for r in resultats if r["statut"] == "ERREUR")

    journaliser(journal_path, f"CSV récapitulatif : {csv_path}")
    journaliser(journal_path, f"Captures OK : {nb_ok}/{NB_CIBLES_ATTENDU}")
    journaliser(journal_path, f"Captures en erreur : {nb_erreurs}/{NB_CIBLES_ATTENDU}")
    journaliser(journal_path, f"Fin capture Index Santé {VERSION_SCRIPT}")

    if nb_erreurs > 0:
        # Le code de sortie non nul fait apparaître le run en échec dans
        # l'onglet Actions, sans empêcher les fichiers déjà écrits d'être
        # commités par l'étape suivante du workflow.
        sys.exit(1)


if __name__ == "__main__":
    main()
