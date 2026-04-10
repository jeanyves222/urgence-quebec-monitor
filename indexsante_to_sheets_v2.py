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


def now_montreal():
    return datetime.now(TZ)


def should_run():
    if os.environ.get("FORCE_RUN", "0") == "1":
        return True
    now = now_montreal()
    return now.hour in ALLOWED_HOURS and now.minute <= 10


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


def fetch_page():
    r = requests.get(URL, timeout=30)
    r.raise_for_status()
    return r.text


def normalize_spaces(s):
    return re.sub(r"\s+", " ", s).strip()


def parse_last_update(text):
    patterns = [
        r"Dernière mise à jour complète des données et des taux\s*:\s*([^\n]+)",
        r"Dernière mise à jour\s*:\s*([^\n]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def extract_number(pattern, text):
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.group(1).replace(" ", "") if m else ""


def extract_duration(pattern, text):
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.group(1).strip().replace(" ", "") if m else ""


def parse_quebec_global(text):
    return {
        "total": extract_number(r"Nombre total de personnes à l'urgence\s*:\s*([0-9\s]+)", text),
        "attente_med": extract_number(r"Nombre de personnes qui attendent de voir un médecin\s*:\s*([0-9\s]+)", text),
        "attente_salle": extract_duration(r"Durée moyenne de séjour des personnes dans la salle d'attente \(de la veille\)\s*:\s*([0-9hmin\s]+)", text),
        "attente_civiere": extract_duration(r"Durée moyenne de séjour des personnes en attente sur une civière \(de la veille\)\s*:\s*([0-9hmin\s]+)", text),
        "civieres_fonc": extract_number(r"Civières fonctionnelles\s*:\s*([0-9\s]+)", text),
        "civieres_occ": extract_number(r"Civières occupées\s*:\s*([0-9\s]+)", text),
        "taux": extract_number(r"Taux d'occupation des civières\s*:\s*([0-9]+)\s*%", text),
        "plus24": extract_number(r"Patients sur civière depuis plus de 24 heures\s*:\s*([0-9\s]+)", text),
        "plus48": extract_number(r"Patients sur civière depuis plus de 48 heures\s*:\s*([0-9\s]+)", text),
    }


def parse_regions_and_installations(soup):
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [normalize_spaces(line) for line in text.split("\n") if normalize_spaces(line)]

    regions = []
    installations = []
    seen_regions = set()
    current_region = ""

    region_candidates = []
    installation_candidates = []

    region_skip_prefixes = (
        "Situation générale au Québec",
        "Tendance 10 derniers jours",
        "Nom de l'installation",
        "Nombre total de personnes à l'urgence",
        "Nombre de personnes qui attendent de voir un médecin",
        "Civières fonctionnelles",
        "Civières occupées",
        "Taux d'occupation des civières",
        "Patients sur civière depuis plus de 24 heures",
        "Patients sur civière depuis plus de 48 heures",
        "Besoin de voir un médecin rapidement",
        "Trouver un GMF",
        "Pour quels motifs",
        "Alternatives aux urgences",
        "Portrait de la liste d'attente",
        "Dernière mise à jour",
        "Répertoire santé",
        "Taux d'occupation et temps d'attente",
    )

    installation_prefixes = (
        "HÔPITAL",
        "CENTRE ",
        "CLSC",
        "CHUS",
        "HÔTEL-DIEU",
        "INSTITUT",
        "PAVILLON",
        "CHSLD",
        "L'HÔTEL-DIEU",
        "Hôpital ",
    )

    region_pattern = re.compile(r"^(.*?)\s+(\d{1,3})\s*%$")
    installation_pattern = re.compile(
        r"^(.*?)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d{1,3})\s*%\s+(\d+)\s+(\d+)$"
    )

    for line in lines:
        if "%" in line and len(region_candidates) < 10:
            region_candidates.append(line)

        if any(line.startswith(prefix) for prefix in installation_prefixes) and len(installation_candidates) < 10:
            installation_candidates.append(line)

        if line.startswith(region_skip_prefixes):
            continue

        region_match = region_pattern.match(line)
        if region_match and not line.startswith(installation_prefixes):
            region_name = region_match.group(1).strip()
            region_taux = region_match.group(2).strip()

            if region_name not in seen_regions:
                regions.append({
                    "region": region_name,
                    "taux_occupation_region": region_taux,
                })
                seen_regions.add(region_name)

            current_region = region_name
            continue

        if not line.startswith(installation_prefixes):
            continue

        inst_match = installation_pattern.match(line)
        if not inst_match:
            continue

        installations.append({
            "region": current_region,
            "installation": inst_match.group(1).strip(),
            "total": inst_match.group(2),
            "attente": inst_match.group(3),
            "civieres_fonc": inst_match.group(4),
            "civieres_occ": inst_match.group(5),
            "taux": inst_match.group(6),
            "plus24": inst_match.group(7),
            "plus48": inst_match.group(8),
        })

    return regions, installations, region_candidates, installation_candidates
   

def main():
    book = connect_sheet()

    if not should_run():
        log(book, "horaire", "INFO", "Execution ignoree: heure locale non ciblee")
        return

    now = now_montreal()
    date = now.strftime("%Y-%m-%d")
    heure = now.strftime("%H:%M:%S")
    horodatage = now.strftime("%Y-%m-%d %H:%M:%S")

    log(book, "debug_regions", "INFO", f"Candidats regions: {' || '.join(region_candidates[:5])}")
    log(book, "debug_installations", "INFO", f"Candidats installations: {' || '.join(installation_candidates[:5])}")
    log(book, "demarrage", "INFO", "Debut du releve")

    html = fetch_page()
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")

    maj = parse_last_update(text)
    qc = parse_quebec_global(text)
    regions, installations, region_candidates, installation_candidates = parse_regions_and_installations(soup)
    

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

    log(book, "ecriture", "SUCCES", f"QC=1 Regions={len(regions)} Installations={len(installations)}")


if __name__ == "__main__":
    main()
