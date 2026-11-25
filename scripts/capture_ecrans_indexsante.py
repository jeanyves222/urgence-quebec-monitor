"""
CAPTURE D'ÉCRAN INDEX SANTÉ — V1.3
----------------------------------------------------------------------------
But : conserver une preuve visuelle horodatée (screenshot pleine page) de la
page nationale d'Index Santé et des 26 installations de la catégorisation
officielle (PIVOT, COMPARABLE, CONTEXTE — voir comparables-fatima), en cas de
litige sur la fiabilité des sources.

Nouveau en V1.2 : une fois par jour, à la fenêtre de 08h55 seulement, les
pages régionales d'Index Santé (« Plus de données pour … ») sont capturées en
plus des 27 cibles. La liste n'est pas figée : le script lit d'abord la page
nationale et y repère tous les liens « Plus de données pour … ». Une région qui
retrouve sa page (ex. Mauricie et Centre-du-Québec) est donc captée sans
modifier le script. Si cette lecture échoue, la liste de secours des 15 pages
connues au 16 novembre 2026 est utilisée, pour ne jamais perdre la capture. Ces pages publient le taux d'occupation régional et la
durée moyenne d'attente régionale calculés par la source elle-même, sur les
30 derniers jours : une capture par jour suffit à en garder tout l'historique,
et une capture manquée se rattrape le lendemain.

Nouveau en V1.3 : les publicités et les bandeaux flottants ne masquent plus
les données. Les requêtes vers les régies publicitaires connues sont bloquées,
et juste avant la capture, les emplacements publicitaires et les éléments
flottants (position fixe ou collante : bannières, bandeaux, fenêtres
superposées) sont masqués. Le contenu de la page — chiffres, tableaux,
graphiques, heure de mise à jour — n'est jamais modifié. Le journal indique,
pour chaque capture, le nombre d'éléments masqués et de requêtes bloquées. Fichiers 30_region_… à
44_region_… pour les 15 régions connues (numéros fixes), 45_region_… et plus
pour toute nouvelle région, dans le même dossier que les autres captures de
08h55.

Conçu pour tourner sur un runner GitHub HÉBERGÉ (ubuntu-24.04), déclenché
toutes les 5 minutes par un appel externe (cron-job.org → workflow_dispatch).
Le script lui-même décide s'il doit RÉELLEMENT capturer : seulement aux mêmes
fenêtres que la Feuille 1, soit :05 et :55 des heures 0h, 8h et 16h, heure de
Montréal — calculé avec ZoneInfo, donc insensible au changement d'heure. Les
autres passages se terminent en une seconde, sans capture.

Anti-doublon (script) : une fenêtre déjà captée aujourd'hui (dossier déjà
présent au démarrage) est sautée. Rattrapage : une fenêtre manquée est encore
captée jusqu'à TOLERANCE_RATTRAPAGE_MIN minutes après l'heure cible, pour
absorber les retards habituels du déclencheur.

Anti-doublon (git, V1.1) : comme une vraie capture dure plusieurs minutes —
plus longtemps que l'intervalle de 5 minutes entre deux appels — plusieurs
exécutions peuvent démarrer avant que la première ait fini et poussé ses
fichiers, chacune constatant (à tort) que le dossier n'existe pas encore.
Pour éviter que deux exécutions capturent la même fenêtre et entrent en
conflit au moment de pousser (conflit git sur des fichiers binaires), le nom
du dossier de sortie est exporté vers GITHUB_ENV : l'étape de commit du
workflow revérifie, juste avant d'ajouter les fichiers, si ce dossier existe
déjà sur origin/main, et abandonne proprement si c'est le cas.

Sortie : captures/AAAA-MM-JJ/HHhMM/ — un PNG par cible (optimisé sans perte
avec Pillow) plus un CSV recapitulatif. Le .yml pousse le tout dans le dépôt.
----------------------------------------------------------------------------
"""

from playwright.sync_api import sync_playwright
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import csv
from urllib.parse import urlparse
import re
import os
import sys
import time
import traceback

from PIL import Image

VERSION_SCRIPT = "V1.3-SANS-PUBLICITE"

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
ROLE_REGIONAL = "REGIONAL"
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

# ----------------------------------------------------------------------------
# PAGES RÉGIONALES (V1.2) — capturées une fois par jour seulement, à la
# fenêtre FENETRE_REGIONALE (08h55, après la mise à jour quotidienne de la
# source). La liste réelle est découverte chaque jour sur la page nationale
# (voir decouvrir_pages_regionales). La liste ci-dessous sert à deux choses :
# donner un numéro de fichier FIXE à chaque région connue, et servir de
# secours si la découverte échoue. Au 16 novembre 2026, la Mauricie et
# Centre-du-Québec n'a pas de lien « Plus de données » : elle n'y figure pas,
# mais sera captée automatiquement (numéro 45 ou plus) si son lien revient.
# ----------------------------------------------------------------------------
FENETRE_REGIONALE = (8, 55)

PAGES_REGIONALES = [
    {
        "code": "30_region_abitibi_temiscamingue",
        "nom": "Page régionale Abitibi-Témiscamingue",
        "url": "https://www.indexsante.ca/urgences/abitibi-temiscamingue.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "31_region_bas_saint_laurent",
        "nom": "Page régionale Bas-Saint-Laurent",
        "url": "https://www.indexsante.ca/urgences/bas-saint-laurent.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "32_region_capitale_nationale",
        "nom": "Page régionale Capitale-Nationale",
        "url": "https://www.indexsante.ca/urgences/capitale-nationale.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "33_region_chaudiere_appalaches",
        "nom": "Page régionale Chaudière-Appalaches",
        "url": "https://www.indexsante.ca/urgences/chaudiere-appalaches.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "34_region_cote_nord",
        "nom": "Page régionale Côte-Nord",
        "url": "https://www.indexsante.ca/urgences/cote-nord.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "35_region_estrie",
        "nom": "Page régionale Estrie",
        "url": "https://www.indexsante.ca/urgences/estrie.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "36_region_gaspesie_iles_de_la_madeleine",
        "nom": "Page régionale Gaspésie-Îles-de-la-Madeleine",
        "url": "https://www.indexsante.ca/urgences/gaspesie-iles-de-la-madeleine.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "37_region_lanaudiere",
        "nom": "Page régionale Lanaudière",
        "url": "https://www.indexsante.ca/urgences/lanaudiere.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "38_region_laurentides",
        "nom": "Page régionale Laurentides",
        "url": "https://www.indexsante.ca/urgences/laurentides.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "39_region_laval",
        "nom": "Page régionale Laval",
        "url": "https://www.indexsante.ca/urgences/laval.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "40_region_monteregie",
        "nom": "Page régionale Montérégie",
        "url": "https://www.indexsante.ca/urgences/monteregie.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "41_region_montreal",
        "nom": "Page régionale Montréal",
        "url": "https://www.indexsante.ca/urgences/montreal.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "42_region_nord_du_quebec",
        "nom": "Page régionale Nord-du-Québec",
        "url": "https://www.indexsante.ca/urgences/nord-du-quebec.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "43_region_outaouais",
        "nom": "Page régionale Outaouais",
        "url": "https://www.indexsante.ca/urgences/outaouais.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
    {
        "code": "44_region_saguenay_lac_saint_jean",
        "nom": "Page régionale Saguenay-Lac-Saint-Jean",
        "url": "https://www.indexsante.ca/urgences/saguenay-lac-saint-jean.php",
        "role": ROLE_REGIONAL,
        "bassin": "",
    },
]

NB_PAGES_REGIONALES_CONNUES = len(PAGES_REGIONALES)  # 15
PREMIER_NUMERO_NOUVELLE_REGION = 45

URL_PAGE_NATIONALE = "https://www.indexsante.ca/urgences/"
MOTIF_URL_REGIONALE = re.compile(r"^https://www\.indexsante\.ca/urgences/([a-z0-9-]+)\.php$")
TEXTE_LIEN_REGIONAL = "Plus de données pour"


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


def exporter_dossier_vers_github_env(dossier_sortie):
    """
    Écrit le chemin (relatif, style posix) du dossier de sortie dans
    GITHUB_ENV, pour que l'étape suivante du workflow (commit/push) puisse
    revérifier juste avant de committer si ce dossier existe déjà sur
    origin/main — voir l'anti-doublon git en tête de fichier. Sans effet
    (et sans erreur) hors d'un run GitHub Actions.
    """
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return
    try:
        with open(github_env, "a", encoding="utf-8") as f:
            f.write(f"DOSSIER_CAPTURE={dossier_sortie.as_posix()}\n")
    except Exception as e:
        print(f"AVERTISSEMENT — écriture GITHUB_ENV a échoué : {e}")


def optimiser_png_sans_perte(chemin_fichier):
    """Recompresse le PNG sans perte (mêmes pixels, fichier plus léger)."""
    try:
        with Image.open(chemin_fichier) as img:
            img.save(chemin_fichier, format="PNG", optimize=True, compress_level=9)
    except Exception as e:
        # Non bloquant : le PNG d'origine reste valide si l'optimisation échoue.
        print(f"AVERTISSEMENT — optimisation PNG a échoué pour {chemin_fichier} : {e}")


# ----------------------------------------------------------------------------
# PUBLICITÉS ET BANDEAUX (V1.3)
# ----------------------------------------------------------------------------
DOMAINES_PUBLICITAIRES = (
    "doubleclick.net", "googlesyndication.com", "googletagservices.com",
    "googleadservices.com", "adservice.google.com", "adservice.google.ca",
    "amazon-adsystem.com", "adnxs.com", "criteo.com", "criteo.net",
    "taboola.com", "outbrain.com", "pubmatic.com", "rubiconproject.com",
    "openx.net", "casalemedia.com", "moatads.com", "adsafeprotected.com",
    "teads.tv", "sharethrough.com", "media.net", "smartadserver.com",
    "fundingchoicesmessages.google.com",
)

JS_MASQUER_PUBLICITES = r"""
() => {
  let n = 0;
  const cacher = (el) => {
    if (!el || el === document.body || el === document.documentElement) return;
    if (el.dataset && el.dataset.msriMasque) return;
    el.style.setProperty('display', 'none', 'important');
    if (el.dataset) el.dataset.msriMasque = '1';
    n++;
  };
  const selecteurs = [
    'ins.adsbygoogle', '.adsbygoogle', '[id^="google_ads"]', '[id^="div-gpt-ad"]',
    '[id^="aswift_"]', 'iframe[src*="doubleclick"]', 'iframe[src*="googlesyndication"]',
    'iframe[id^="google_ads"]', '[data-google-query-id]', '.google-auto-placed',
    '.fc-consent-root', '.fc-dialog-overlay'
  ];
  document.querySelectorAll(selecteurs.join(',')).forEach(cacher);
  document.querySelectorAll('body *').forEach((el) => {
    const pos = getComputedStyle(el).position;
    if (pos === 'fixed' || pos === 'sticky') cacher(el);
  });
  document.documentElement.style.setProperty('overflow', 'visible', 'important');
  document.body.style.setProperty('overflow', 'visible', 'important');
  return n;
}
"""


def est_requete_publicitaire(url):
    try:
        hote = urlparse(url).hostname or ""
    except Exception:
        return False
    return any(hote == d or hote.endswith("." + d) for d in DOMAINES_PUBLICITAIRES)


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
            bloquees = [0]

            def filtrer(route):
                if est_requete_publicitaire(route.request.url):
                    bloquees[0] += 1
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", filtrer)
            page = context.new_page()
            page.goto(cible["url"], wait_until="domcontentloaded", timeout=TIMEOUT_NAVIGATION_MS)
            page.wait_for_timeout(ATTENTE_APRES_CHARGEMENT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=TIMEOUT_NETWORKIDLE_MS)
            except Exception:
                pass
            try:
                nb_masques = page.evaluate(JS_MASQUER_PUBLICITES)
                page.wait_for_timeout(500)
            except Exception as e_masque:
                nb_masques = f"échec ({type(e_masque).__name__})"
            journaliser(journal_path, f"Publicités et bandeaux masqués : {nb_masques} ; requêtes publicitaires bloquées : {bloquees[0]}")
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


def decouvrir_pages_regionales(playwright, journal_path):
    """
    Lit la page nationale et retourne la liste des pages régionales à capturer,
    d'après les liens « Plus de données pour … ». Les régions connues gardent
    leur numéro fixe (30 à 44) ; une région nouvelle reçoit le numéro 45 ou
    plus. En cas d'échec (aucun lien trouvé, page inaccessible), retourne la
    liste de secours PAGES_REGIONALES — la capture n'est jamais perdue.
    """
    connues = {}
    for page_reg in PAGES_REGIONALES:
        m = MOTIF_URL_REGIONALE.match(page_reg["url"])
        connues[m.group(1)] = page_reg

    for tentative in range(1, MAX_TENTATIVES + 1):
        browser = None
        try:
            journaliser(journal_path, f"Recherche des pages régionales sur la page nationale | tentative {tentative}")
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(locale="fr-CA", ignore_https_errors=True)
            page = context.new_page()
            page.goto(URL_PAGE_NATIONALE, wait_until="domcontentloaded", timeout=TIMEOUT_NAVIGATION_MS)
            page.wait_for_timeout(ATTENTE_APRES_CHARGEMENT_MS)
            liens = page.eval_on_selector_all(
                "a", "els => els.map(e => [(e.textContent || '').trim(), e.href || ''])"
            )
            context.close()
            browser.close()
            browser = None

            trouvees = []
            slugs_vus = set()
            for texte, href in liens:
                if not texte.startswith(TEXTE_LIEN_REGIONAL):
                    continue
                m = MOTIF_URL_REGIONALE.match(href.strip())
                if not m or m.group(1) in slugs_vus:
                    continue
                slugs_vus.add(m.group(1))
                nom_region = texte[len(TEXTE_LIEN_REGIONAL):].strip() or m.group(1)
                trouvees.append((m.group(1), nom_region, href.strip()))

            if not trouvees:
                raise RuntimeError("aucun lien « Plus de données pour … » trouvé sur la page nationale")

            resultat = []
            nouvelles = []
            for slug, nom_region, url in trouvees:
                if slug in connues:
                    resultat.append(connues[slug])
                else:
                    nouvelles.append((slug, nom_region, url))
            for k, (slug, nom_region, url) in enumerate(sorted(nouvelles)):
                resultat.append({
                    "code": f"{PREMIER_NUMERO_NOUVELLE_REGION + k}_region_{slug.replace('-', '_')}",
                    "nom": f"Page régionale {nom_region}",
                    "url": url,
                    "role": ROLE_REGIONAL,
                    "bassin": "",
                })
            resultat.sort(key=lambda c: c["code"])

            absentes = sorted(set(connues) - slugs_vus)
            journaliser(journal_path, f"Pages régionales trouvées : {len(resultat)} (connues : {len(resultat) - len(nouvelles)}, nouvelles : {len(nouvelles)})")
            for slug, nom_region, url in sorted(nouvelles):
                journaliser(journal_path, f"NOUVELLE page régionale : {nom_region} — {url}")
            for slug in absentes:
                journaliser(journal_path, f"Page régionale connue absente aujourd'hui : {connues[slug]['nom']}")
            return resultat
        except Exception as e:
            journaliser(journal_path, f"ERREUR recherche des pages régionales, tentative {tentative} : {e}")
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            if tentative < MAX_TENTATIVES:
                time.sleep(PAUSE_ENTRE_TENTATIVES_S)

    journaliser(journal_path, f"Recherche impossible : liste de secours utilisée ({NB_PAGES_REGIONALES_CONNUES} pages régionales)")
    return list(PAGES_REGIONALES)


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

    # Anti-doublon git (V1.1) : on prévient l'étape de commit du chemin exact
    # du dossier qu'on s'apprête à remplir, pour qu'elle puisse revérifier
    # juste avant de committer si une AUTRE exécution, démarrée entre-temps,
    # a déjà poussé ce même dossier — voir l'en-tête du fichier.
    exporter_dossier_vers_github_env(dossier_sortie)

    dossier_sortie.mkdir(parents=True, exist_ok=True)
    journal_path = dossier_sortie / "journal_capture.txt"

    journaliser(journal_path, f"Début capture Index Santé {VERSION_SCRIPT}" + (" (FORCÉE — test manuel)" if forcer else ""))
    if not forcer:
        journaliser(journal_path, f"Fenêtre cible : {cible_fenetre:%Y-%m-%d %Hh%M} (heure de Montréal)")
    journaliser(journal_path, f"Heure réelle de déclenchement : {maintenant:%Y-%m-%d %H:%M:%S}")
    journaliser(journal_path, f"Dossier de sortie : {dossier_sortie}")

    # V1.2 : les pages régionales s'ajoutent aux 27 cibles à la seule fenêtre
    # de 08h55 (pas lors d'une capture forcée de test). Leur liste est
    # découverte sur la page nationale, avec une liste de secours.
    pages_du_jour = []
    fenetre_regionale = (not forcer) and (cible_fenetre.hour, cible_fenetre.minute) == FENETRE_REGIONALE

    resultats = []
    with sync_playwright() as p:
        if fenetre_regionale:
            pages_du_jour = decouvrir_pages_regionales(p, journal_path)
            journaliser(journal_path, f"Fenêtre de 08h55 : ajout de {len(pages_du_jour)} pages régionales")
        liste_a_capturer = list(CIBLES) + pages_du_jour
        nb_attendu = len(liste_a_capturer)
        for cible in liste_a_capturer:
            resultats.append(capturer_une_cible(p, cible, dossier_sortie, journal_path))

    csv_path = ecrire_csv(dossier_sortie, resultats)

    nb_ok = sum(1 for r in resultats if r["statut"] == "OK")
    nb_erreurs = sum(1 for r in resultats if r["statut"] == "ERREUR")

    journaliser(journal_path, f"CSV récapitulatif : {csv_path}")
    journaliser(journal_path, f"Captures OK : {nb_ok}/{nb_attendu}")
    journaliser(journal_path, f"Captures en erreur : {nb_erreurs}/{nb_attendu}")
    journaliser(journal_path, f"Fin capture Index Santé {VERSION_SCRIPT}")

    if nb_ok == 0:
        # On ne fait échouer visiblement le run (croix rouge + courriel
        # d'alerte GitHub) que si RIEN n'a fonctionné — signe probable d'un
        # vrai problème (script, réseau, environnement). Une ou quelques
        # cibles en erreur parmi 27 est une situation normale (site source
        # temporairement indisponible pour UNE installation) : le CSV et le
        # journal la documentent déjà, ce n'est pas un échec du dispositif.
        sys.exit(1)


if __name__ == "__main__":
    main()
