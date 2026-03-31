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


def parse_service_account():
    raw = os.environ["GCP_SERVICE_ACCOUNT_JSON"]
    return json.loads(raw)


def connect_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(parse_service_account(), scope)
    client = gspread.authorize(credentials)
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    return client.open_by_key(sheet_id)


def append_row(ws, row):
    ws.append_row(row, value_input_option="USER_ENTERED")


def log(sheet, etape, statut, details):
    ws = sheet.worksheet("Journal_Technique")
    ts = now_montreal().strftime("%Y-%m-%d %H:%M:%S")
    append_row(ws, [ts, etape, statut, details[:49000]])


def should_run():
    if os.environ.get("FORCE_RUN", "0") == "1":
        return True
    return now_montreal().hour in ALLOWED_HOURS


def clean_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def to_int(text):
    if text is None:
        return ""
    t = re.sub(r"[^\d]", "", str(text))
    return int(t) if t else ""


def to_percent(text):
    if text is None:
        return ""
    m = re.search(r"(\d+)\s*%", str(text))
    return int(m.group(1)) if m else ""


def fetch_page():
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(URL, timeout=60, headers=headers)
    resp.raise_for_status()
    return resp.text


def parse_last_update(soup):
    text = soup.get_text("\n", strip=True)
    patterns = [
        r"Dernière mise à jour\s*:\s*([^\n]+)",
        r"Derniere mise a jour\s*:\s*([^\n]+)",
        r"Mise à jour\s*:\s*([^\n]+)",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return clean_text(m.group(1))
    return ""


def parse_quebec_global(text):
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]
    joined = "\n".join(lines)
    data = {
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
    patterns = {
        "total_urgence_quebec": r"Total à l'urgence[^0-9]*([0-9][0-9\s]*)",
        "attente_medecin_quebec": r"En attente de voir un médecin[^0-9]*([0-9][0-9\s]*)",
        "attente_salle_quebec": r"Salle d'attente[^0-9]*([0-9][0-9\s]*)",
        "attente_civiere_quebec": r"En attente sur civière[^0-9]*([0-9][0-9\s]*)",
        "civieres_fonctionnelles_quebec": r"Civières fonctionnelles[^0-9]*([0-9][0-9\s]*)",
        "civieres_occupees_quebec": r"Civières occupées[^0-9]*([0-9][0-9\s]*)",
        "taux_occupation_quebec": r"Taux d'occupation[^0-9]*([0-9]{1,3})\s*%",
        "plus_24h_quebec": r">\s*24h[^0-9]*([0-9][0-9\s]*)",
        "plus_48h_quebec": r">\s*48h[^0-9]*([0-9][0-9\s]*)",
        }
    for key, pat in patterns.items():
        m = re.search(pat, joined, flags=re.IGNORECASE)
        if m:
            data[key] = to_int(m.group(1)) if "taux" not in key else int(m.group(1))
    return data


def parse_regions_and_installations(soup):
    # This parser aims to stay flexible: it scans headings and nearby tables/cards.
    text = soup.get_text("\n", strip=True)
    # split by known region headings if available
    region_names = [
        "Bas-Saint-Laurent","Saguenay-Lac-Saint-Jean","Capitale-Nationale","Mauricie-et-Centre-du-Québec",
        "Estrie","Montréal","Outaouais","Abitibi-Témiscamingue","Côte-Nord","Nord-du-Québec",
        "Gaspésie-Îles-de-la-Madeleine","Chaudière-Appalaches","Laval","Lanaudière","Laurentides","Montérégie"
    ]
    regions = []
    installations = []

    whole_text = soup.get_text("\n", strip=True)
    for region in region_names:
        if region not in whole_text:
            continue
        # attempt to find region occupancy near heading
        region_pattern = rf"{re.escape(region)}(?:(?!{ '|'.join(map(re.escape, region_names)) }).)*?([0-9]{{1,3}})\s*%"
        m = re.search(region_pattern, whole_text, flags=re.IGNORECASE | re.DOTALL)
        taux = int(m.group(1)) if m else ""
        regions.append({"region": region, "taux_occupation_region": taux})

    # Generic row parser from tables
    for table in soup.find_all("table"):
        headers = [clean_text(th.get_text(" ", strip=True)).lower() for th in table.find_all(["th","td"])[:12]]
        joined_headers = " | ".join(headers)
        if not ("occupation" in joined_headers and ("civi" in joined_headers or "médecin" in joined_headers or "medecin" in joined_headers)):
            continue
        # detect region title before table
        region = ""
        prev = table
        for _ in range(8):
            prev = prev.find_previous(["h1","h2","h3","h4","strong","b","div"])
            if not prev:
                break
            candidate = clean_text(prev.get_text(" ", strip=True))
            if candidate in region_names:
                region = candidate
                break
        rows = table.find_all("tr")
        for tr in rows[1:]:
            cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td","th"])]
            if len(cells) < 4:
                continue
            installation = cells[0]
            nums = cells[1:]
            parsed = [to_int(x) if "%" not in x else to_percent(x) for x in nums]
            row = {
                "region": region,
                "installation": installation,
                "total_urgence": parsed[0] if len(parsed) > 0 else "",
                "attente_medecin": parsed[1] if len(parsed) > 1 else "",
                "civieres_fonctionnelles": parsed[2] if len(parsed) > 2 else "",
                "civieres_occupees": parsed[3] if len(parsed) > 3 else "",
                "taux_occupation": parsed[4] if len(parsed) > 4 else "",
                "plus_24h": parsed[5] if len(parsed) > 5 else "",
                "plus_48h": parsed[6] if len(parsed) > 6 else "",
            }
            if installation and any(v != "" for k, v in row.items() if k not in {"region", "installation"}):
                installations.append(row)

    # Fallback card parser if tables were not found
    if not installations:
        lines = [clean_text(x) for x in whole_text.splitlines() if clean_text(x)]
        current_region = ""
        for i, line in enumerate(lines):
            if line in region_names:
                current_region = line
                continue
            # A likely installation line followed by metrics lines
            if current_region and len(line) > 3 and not re.fullmatch(r"\d+%?", line):
                window = " | ".join(lines[i:i+8])
                if any(tok in window.lower() for tok in ["occupation", "civ", "médecin", "medecin", ">24", ">48"]):
                    # extract first 7 metrics found in window after the installation label
                    metrics = re.findall(r"(\d+\s*%?|\d+\s*h\d+)", window)
                    vals = []
                    for m in metrics[:7]:
                        vals.append(to_percent(m) if "%" in m else to_int(m))
                    if vals:
                        installations.append({
                            "region": current_region,
                            "installation": line,
                            "total_urgence": vals[0] if len(vals) > 0 else "",
                            "attente_medecin": vals[1] if len(vals) > 1 else "",
                            "civieres_fonctionnelles": vals[2] if len(vals) > 2 else "",
                            "civieres_occupees": vals[3] if len(vals) > 3 else "",
                            "taux_occupation": vals[4] if len(vals) > 4 else "",
                            "plus_24h": vals[5] if len(vals) > 5 else "",
                            "plus_48h": vals[6] if len(vals) > 6 else "",
                        })

    # De-duplicate
    uniq = []
    seen = set()
    for row in installations:
        key = (row.get("region",""), row.get("installation",""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)
    return regions, uniq


def main():
    book = connect_sheet()

    if os.environ.get("TEST_WRITE_ONLY", "0") == "1":
        log(book, "connexion_google_sheets", "SUCCES", "Test d'ecriture simple reussi.")
        return

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

    # Sheets
    ws_qc = book.worksheet("Quebec_Global")
    ws_regions = book.worksheet("Regions")
    ws_inst = book.worksheet("Installations")

    append_row(ws_qc, [
        date_releve, heure_releve, horodatage, derniere_mise_a_jour_site,
        q["total_urgence_quebec"], q["attente_medecin_quebec"], q["attente_salle_quebec"], q["attente_civiere_quebec"],
        q["civieres_fonctionnelles_quebec"], q["civieres_occupees_quebec"], q["taux_occupation_quebec"],
        q["plus_24h_quebec"], q["plus_48h_quebec"], URL
    ])

    for row in regions:
        append_row(ws_regions, [
            date_releve, heure_releve, horodatage, derniere_mise_a_jour_site,
            row.get("region",""), row.get("taux_occupation_region",""), URL
        ])

    for row in installations:
        append_row(ws_inst, [
            date_releve, heure_releve, horodatage, derniere_mise_a_jour_site,
            row.get("region",""), row.get("installation",""), row.get("total_urgence",""),
            row.get("attente_medecin",""), row.get("civieres_fonctionnelles",""),
            row.get("civieres_occupees",""), row.get("taux_occupation",""),
            row.get("plus_24h",""), row.get("plus_48h",""), URL
        ])

    log(
        book,
        "ecriture_terminee",
        "SUCCES",
        f"Lignes ecrites - Quebec_Global: 1, Regions: {len(regions)}, Installations: {len(installations)}"
    )


if __name__ == "__main__":
    main()
