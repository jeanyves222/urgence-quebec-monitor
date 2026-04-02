import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import requests
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

# CONFIG
URL = "https://www.indexsante.ca/urgences/#Bas-Saint-Laurent"
TZ = ZoneInfo("America/Montreal")
ALLOWED_HOURS = {0, 8, 16}

# =========================
# TEMPS & EXECUTION
# =========================

def now_montreal():
    return datetime.now(TZ)

def should_run():
    if os.environ.get("FORCE_RUN", "0") == "1":
        return True

    now = now_montreal()
    return now.hour in ALLOWED_HOURS and now.minute <= 10

# =========================
# GOOGLE SHEETS
# =========================

def connect_sheet():
    creds_json = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    return client.open_by_key(sheet_id)

def append_row(ws, row):
    ws.append_row(row, value_input_option="USER_ENTERED")

def log(book, etape, niveau, message):
    ws = book.worksheet("Journal_Technique")
    now = now_montreal().strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([now, etape, niveau, message])

# =========================
# SCRAPING
# =========================

def fetch_page():
    response = requests.get(URL, timeout=30)
    response.raise_for_status()
    return response.text

def parse_last_update(soup):
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Dernière mise à jour\s*:\s*(.+)", text)
    return match.group(1) if match else ""

def parse_quebec_global(text):
    return {
        "total_urgence_quebec": "",
        "attente_medecin_quebec": "",
        "attente_salle_quebec": "",
        "attente_civiere_quebec": "",
        "civieres_fonctionnelles_quebec": "",
        "civieres_occupees_quebec": "",
        "taux_occupation_quebec": "",
        "plus_24h_quebec": "",
        "plus_48h_quebec": "",
    }

def parse_regions_and_installations(soup):
    # Version simple (à améliorer en phase 2)
    return [], []

# =========================
# MAIN
# =========================

def main():
    book = connect_sheet()

    if not should_run():
        log(book, "horaire", "INFO", "Execution ignoree: heure locale non ciblee et FORCE_RUN=0.")
        return

    now = now_montreal()
    date_releve = now.strftime("%Y-%m-%d")
    heure_releve = now.strftime("%H:%M:%S")
    horodatage = now.strftime("%Y-%m-%d %H:%M:%S")

    log(book, "demarrage", "INFO", "Debut du releve.")

    html = fetch_page()
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    derniere_mise_a_jour_site = parse_last_update(soup)
    q = parse_quebec_global(text)
    regions, installations = parse_regions_and_installations(soup)

    # SHEETS
    ws_qc = book.worksheet("Quebec_Global")
    ws_regions = book.worksheet("Regions")
    ws_inst = book.worksheet("Installations")

    append_row(ws_qc, [
        date_releve, heure_releve, horodatage, derniere_mise_a_jour_site,
        q["total_urgence_quebec"], q["attente_medecin_quebec"],
        q["attente_salle_quebec"], q["attente_civiere_quebec"],
        q["civieres_fonctionnelles_quebec"], q["civieres_occupees_quebec"],
        q["taux_occupation_quebec"], q["plus_24h_quebec"],
        q["plus_48h_quebec"], URL
    ])

    for row in regions:
        append_row(ws_regions, [
            date_releve, heure_releve, horodatage, derniere_mise_a_jour_site,
            row.get("region", ""), row.get("taux_occupation_region", ""), URL
        ])

    for row in installations:
        append_row(ws_inst, [
            date_releve, heure_releve, horodatage, derniere_mise_a_jour_site,
            row.get("region", ""), row.get("installation", ""),
            row.get("total_urgence", ""), row.get("attente_medecin", ""),
            row.get("civieres_fonctionnelles", ""), row.get("civieres_occupees", ""),
            row.get("taux_occupation", ""), row.get("plus_24h", ""),
            row.get("plus_48h", ""), URL
        ])

    log(
        book,
        "ecriture_terminee",
        "SUCCES",
        f"Lignes ecrites - Quebec_Global: 1, Regions: {len(regions)}, Installations: {len(installations)}"
    )

if __name__ == "__main__":
    main()
