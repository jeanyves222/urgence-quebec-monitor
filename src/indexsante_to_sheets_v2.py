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


def get_creneau_releve():
    now = now_montreal()

    # Rattrapage élargi :
    # 00h accepté de 00:00 à 02:59
    # 08h accepté de 08:00 à 10:59
    # 16h accepté de 16:00 à 18:59

    if now.hour in [0, 1, 2]:
        return f"{now.strftime('%Y-%m-%d')}_00"

    if now.hour in [8, 9, 10]:
        return f"{now.strftime('%Y-%m-%d')}_08"

    if now.hour in [16, 17, 18]:
        return f"{now.strftime('%Y-%m-%d')}_16"

    return None



def should_run():
    if os.environ.get("FORCE_RUN", "0") == "1":
        return True

    return get_creneau_releve() is not None


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


def append_rows_batch(ws, rows):
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def log(book, etape, niveau, message):
    ws = book.worksheet("Journal_Technique")
    ws.append_row([now_montreal().strftime("%Y-%m-%d %H:%M:%S"), etape, niveau, message])


def creneau_deja_present(book, creneau):
    ws = book.worksheet("Journal_Technique")
    values = ws.get_all_values()

    for row in values:
        if len(row) >= 4 and creneau in row[3]:
            return True

    return False


def fetch_page():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(URL, headers=headers, timeout=30)
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
    text = soup.get_text(" ", strip=True).replace("\xa0", " ")
    text = normalize_spaces(text)

    region_names = [
        "Abitibi-Témiscamingue",
        "Bas-Saint-Laurent",
        "Capitale-Nationale",
        "Chaudière-Appalaches",
        "Côte-Nord",
        "Estrie",
        "Gaspésie-Îles-de-la-Madeleine",
        "Lanaudière",
        "Laurentides",
        "Laval",
        "Mauricie et Centre-du-Québec",
        "Montérégie",
        "Montréal",
        "Nord-du-Québec",
        "Outaouais",
        "Saguenay-Lac-Saint-Jean",
    ]

    regions = []
    installations = []
    region_hits = []

    for region in region_names:
        pattern = re.compile(
            rf"{re.escape(region)}\s+(\d{{1,3}})\s*%\s+Plus de données pour\s+{re.escape(region)}",
            flags=re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            region_hits.append({
                "region": region,
                "taux": m.group(1),
                "start": m.start(),
            })

    region_hits.sort(key=lambda x: x["start"])

    installation_pattern = re.compile(
        r"(HÔPITAL|CENTRE|CLSC|CHUS|HÔTEL-DIEU|INSTITUT|PAVILLON|CHSLD|L'HÔTEL-DIEU|Hôpital)"
        r"(.*?)"
        r"\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d{1,3})\s*%\s+(\d+)\s+(\d+)"
    )

    for idx, region_info in enumerate(region_hits):
        region = region_info["region"]
        taux = region_info["taux"]
        start = region_info["start"]
        end = region_hits[idx + 1]["start"] if idx + 1 < len(region_hits) else len(text)

        block = text[start:end]

        regions.append({
            "region": region,
            "taux_occupation_region": taux,
        })

        for match in installation_pattern.finditer(block):
            prefix = match.group(1)
            rest = match.group(2).strip()
            installation_name = f"{prefix} {rest}".strip()

            installations.append({
                "region": region,
                "installation": installation_name,
                "total": match.group(3),
                "attente": match.group(4),
                "civieres_fonc": match.group(5),
                "civieres_occ": match.group(6),
                "taux": match.group(7),
                "plus24": match.group(8),
                "plus48": match.group(9),
            })

    return regions, installations


def main():
    book = connect_sheet()

    creneau = get_creneau_releve()

    if os.environ.get("FORCE_RUN", "0") == "1":
        creneau = f"FORCE_RUN_{now_montreal().strftime('%Y-%m-%d_%H-%M-%S')}"

    if not should_run():
        now_check = now_montreal()
        log(
            book,
            "horaire",
            "INFO",
            f"Execution ignoree | heure={now_check.hour} | minute={now_check.minute} | creneau=AUCUN"
        )
        return

    if creneau_deja_present(book, creneau):
        log(book, "doublon", "INFO", f"Execution ignoree | creneau deja present: {creneau}")
        return

    now = now_montreal()
    date = now.strftime("%Y-%m-%d")
    heure = now.strftime("%H:%M:%S")
    horodatage = now.strftime("%Y-%m-%d %H:%M:%S")

    log(book, "demarrage", "INFO", f"Debut du releve | creneau={creneau}")

    html = fetch_page()
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")

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

    region_rows = []
    for r in regions:
        region_rows.append([
            date, heure, horodatage, maj,
            r["region"], r["taux_occupation_region"], URL
        ])

    installation_rows = []
    for i in installations:
        installation_rows.append([
            date, heure, horodatage, maj,
            i["region"], i["installation"], i["total"],
            i["attente"], i["civieres_fonc"], i["civieres_occ"],
            i["taux"], i["plus24"], i["plus48"], URL
        ])

    append_rows_batch(ws_regions, region_rows)
    append_rows_batch(ws_inst, installation_rows)

    log(
        book,
        "debug",
        "INFO",
        f"creneau={creneau} | Premiere region: {regions[0]['region'] if regions else 'AUCUNE'} | Premiere installation: {installations[0]['installation'] if installations else 'AUCUNE'}"
    )

    log(
        book,
        "ecriture",
        "SUCCES",
        f"creneau={creneau} | QC=1 Regions={len(regions)} Installations={len(installations)}"
    )


if __name__ == "__main__":
    main()