import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import requests
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

URL = "https://www.indexsante.ca/urgences/#Bas-Saint-Laurent"
TZ = ZoneInfo("America/Montreal")
ALLOWED_HOURS = {0, 8, 16}

# =========================
# TEMPS
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
    return client.open_by_key(os.environ["GOOGLE_SHEET_ID"])

def append_row(ws, row):
    ws.append_row(row, value_input_option="USER_ENTERED")

def log(book, etape, niveau, message):
    ws = book.worksheet("Journal_Technique")
    ws.append_row([now_montreal().strftime("%Y-%m-%d %H:%M:%S"), etape, niveau, message])

# =========================
# SCRAPING
# =========================

def fetch_page():
    r = requests.get(URL, timeout=30)
    r.raise_for_status()
    return r.text

def parse_last_update(text):
    m = re.search(r"Dernière mise à jour.*?:\s*(.+)", text)
    return m.group(1) if m else ""

def extract_number(pattern, text):
    m = re.search(pattern, text)
    return m.group(1).replace(" ", "") if m else ""

def parse_quebec_global(text):
    return {
        "total": extract_number(r"Nombre total de personnes à l'urgence\s*:\s*([0-9\s]+)", text),
        "attente_med": extract_number(r"attendent de voir un médecin\s*:\s*([0-9\s]+)", text),
        "attente_salle": extract_number(r"salle d'attente.*?:\s*([0-9h\s]+)", text),
        "attente_civiere": extract_number(r"civière.*?:\s*([0-9h\s]+)", text),
        "civieres_fonc": extract_number(r"Civières fonctionnelles\s*:\s*([0-9\s]+)", text),
        "civieres_occ": extract_number(r"Civières occupées\s*:\s*([0-9\s]+)", text),
        "taux": extract_number(r"Taux d'occupation.*?:\s*([0-9]+)", text),
        "plus24": extract_number(r"plus de 24 heures\s*:\s*([0-9\s]+)", text),
        "plus48": extract_number(r"plus de 48 heures\s*:\s*([0-9\s]+)", text),
    }

# =========================
# PARSEUR REGIONS + INSTALLATIONS
# =========================

def parse_regions_and_installations(soup):
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    regions = []
    installations = []

    current_region = None
    seen_regions = set()

    region_exclude_prefixes = {
        "Situation générale",
        "Tendance",
        "Nom de l'installation",
        "Nombre total de personnes",
        "Nombre de personnes qui attendent",
        "Civières fonctionnelles",
        "Civières occupées",
        "Taux d'occupation",
        "Patients sur civière",
        "Besoin de voir un médecin",
        "Trouver un GMF",
        "Pour quels motifs",
        "Alternatives aux urgences",
        "Portrait de la liste d'attente",
        "Dernière mise à jour",
        "Répertoire santé",
        "Taux d'occupation et temps d'attente",
    }

    def is_region_line(line):
        if "%" not in line:
            return False
        if any(line.startswith(prefix) for prefix in region_exclude_prefixes):
            return False
        if any(keyword in line for keyword in [
            "HÔPITAL", "CENTRE ", "CLSC", "CHUS", "HÔTEL-DIEU",
            "INSTITUT", "PAVILLON", "CHSLD"
        ]):
            return False
        return re.match(r"^(.*?)\s+(\d{1,3})\s*%$", line) is not None

    def parse_region_line(line):
        m = re.match(r"^(.*?)\s+(\d{1,3})\s*%$", line)
        return {
            "region": m.group(1).strip(),
            "taux_occupation_region": m.group(2).strip()
        }

    installation_keywords = [
        "HÔPITAL", "CENTRE ", "CLSC", "CHUS", "HÔTEL-DIEU",
        "INSTITUT", "PAVILLON", "CHSLD"
    ]

    def is_installation_line(line):
        if not any(k in line for k in installation_keywords):
            return False
        nums = re.findall(r"\d+", line)
        return len(nums) >= 7

    def parse_installation_line(line, current_region):
        nums = re.findall(r"\d+", line)

        name = re.sub(r"\s+\d+(?:\s+\d+){6}\s*$", "", line).strip()

        return {
            "region": current_region or "",
            "installation": name,
            "total": nums[0],
            "attente": nums[1],
            "civieres_fonc": nums[2],
            "civieres_occ": nums[3],
            "taux": nums[4],
            "plus24": nums[5],
            "plus48": nums[6],
        }

    for line in lines:
        if is_region_line(line):
            region_data = parse_region_line(line)
            if region_data["region"] not in seen_regions:
                current_region = region_data["region"]
                regions.append(region_data)
                seen_regions.add(region_data["region"])
            continue

        if is_installation_line(line):
            inst = parse_installation_line(line, current_region)
            installations.append(inst)

    return regions, installations

# =========================
# MAIN
# =========================

def main():
    book = connect_sheet()

    if not should_run():
        log(book, "horaire", "INFO", "Execution ignoree: heure locale non ciblee")
        return

    now = now_montreal()
    date = now.strftime("%Y-%m-%d")
    heure = now.strftime("%H:%M:%S")
    horodatage = now.strftime("%Y-%m-%d %H:%M:%S")

    log(book, "demarrage", "INFO", "Debut du releve")

    html = fetch_page()
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    maj = parse_last_update(text)
    qc = parse_quebec_global(text)
    regions, installations = parse_regions_and_installations(soup)

    ws_qc = book.worksheet("Quebec_Global")
    ws_regions = book.worksheet("Regions")
    ws_inst = book.worksheet("Installations")

    append_row(ws_qc, [
        date, heure, horodatage, maj,
        qc["total"], qc["attente_med"], qc["attente_salle"],
        qc["attente_civiere"], qc["civieres_fonc"], qc["civieres_occ"],
        qc["taux"], qc["plus24"], qc["plus48"], URL
    ])

    for r in regions:
        append_row(ws_regions, [
            date, heure, horodatage, maj,
            r["region"], r["taux_occupation_region"], URL
        ])

    for i in installations:
        append_row(ws_inst, [
            date, heure, horodatage, maj,
            i["region"], i["installation"], i["total"],
            i["attente"], i["civieres_fonc"], i["civieres_occ"],
            i["taux"], i["plus24"], i["plus48"], URL
        ])

    log(book, "ecriture", "SUCCES",
        f"QC=1 Regions={len(regions)} Installations={len(installations)}")

if __name__ == "__main__":
    main()
