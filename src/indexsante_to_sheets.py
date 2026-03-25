#!/usr/bin/env python3
import os
import re
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import gspread

URL = "https://www.indexsante.ca/urgences/"
LOCAL_TZ = os.getenv("LOCAL_TIMEZONE", "America/Montreal")
TARGET_HOURS = {int(x) for x in os.getenv("TARGET_HOURS_LOCAL", "8,16,22").split(",") if x.strip()}
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])

REGIONS = [
    "Abitibi-Témiscamingue", "Bas-Saint-Laurent", "Capitale-Nationale",
    "Chaudière-Appalaches", "Côte-Nord", "Estrie",
    "Gaspésie-Îles-de-la-Madeleine", "Lanaudière", "Laurentides", "Laval",
    "Mauricie et Centre-du-Québec", "Montérégie", "Montréal", "Outaouais",
    "Saguenay-Lac-Saint-Jean"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def now_local():
    return datetime.now(ZoneInfo(LOCAL_TZ))

def should_run_now():
    return now_local().hour in TARGET_HOURS

def clean_lines(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    return [x for x in lines if x]

def parse_global(lines):
    text = "\n".join(lines[:260])
    def grab(pattern):
        m = re.search(pattern, text, re.I)
        return m.group(1).strip() if m else ""
    return {
        "derniere_mise_a_jour_site": grab(r"Dernière mise à jour\s*:\s*([^\n]+)"),
        "nb_total_urgence": grab(r"Nombre total de personnes à l'urgence\s*:\s*(\d+)"),
        "nb_attente_medecin": grab(r"Nombre de personnes qui attendent de voir un médecin\s*:\s*(\d+)"),
        "duree_salle_attente": grab(r"Durée moyenne de séjour des personnes dans la salle d'attente \(de la veille\)\s*:\s*([^\n]+)"),
        "duree_attente_civiere": grab(r"Durée moyenne de séjour des personnes en attente sur une civière \(de la veille\)\s*:\s*([^\n]+)"),
        "civieres_fonctionnelles": grab(r"Civières fonctionnelles\s*:\s*(\d+)"),
        "civieres_occupees": grab(r"Civières occupées\s*:\s*(\d+)"),
        "taux_occupation": grab(r"Taux d'occupation des civières\s*:\s*(\d+%)"),
        "patients_plus_24h": grab(r"Patients sur civière depuis plus de 24 heures\s*:\s*(\d+)"),
        "patients_plus_48h": grab(r"Patients sur civière depuis plus de 48 heures\s*:\s*(\d+)"),
    }

def find_region_blocks(lines):
    start_after = 0
    for idx, line in enumerate(lines):
        if "Patients sur civière depuis plus de 48 heures" in line:
            start_after = idx
            break
    occurrences = []
    cursor = start_after
    for region in REGIONS:
        found = None
        for i in range(cursor, len(lines)):
            if lines[i] == region:
                found = i
                break
        if found is None:
            logging.warning("Région non trouvée: %s", region)
            continue
        occurrences.append((region, found))
        cursor = found + 1
    blocks = {}
    for idx, (region, start) in enumerate(occurrences):
        end = occurrences[idx + 1][1] if idx + 1 < len(occurrences) else len(lines)
        blocks[region] = lines[start:end]
    return blocks

INSTALL_RE = re.compile(r"^(?P<name>.+?)\s+(?P<total>\d+)\s+(?P<attente>\d+)\s+(?P<civf>\d+)\s+(?P<civo>\d+)\s+(?P<taux>\d+%)\s+(?P<p24>\d+)\s+(?P<p48>\d+)$")

def parse_region(region, block_lines):
    region_rate = ""
    installations = []
    for line in block_lines[:8]:
        if re.fullmatch(r"\d+%", line):
            region_rate = line
            break
    for line in block_lines:
        m = INSTALL_RE.match(line)
        if m:
            d = m.groupdict()
            installations.append({
                "installation": d["name"],
                "nb_total_urgence": int(d["total"]),
                "nb_attente_medecin": int(d["attente"]),
                "civieres_fonctionnelles": int(d["civf"]),
                "civieres_occupees": int(d["civo"]),
                "taux_occupation": d["taux"],
                "patients_plus_24h": int(d["p24"]),
                "patients_plus_48h": int(d["p48"]),
            })
    return {"region": region, "taux_region": region_rate, "installations": installations}

def auth_sheet():
    gc = gspread.service_account_from_dict(SERVICE_ACCOUNT_INFO)
    return gc.open_by_key(GOOGLE_SHEET_ID)

def ensure_worksheet(sh, title, headers):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(20, len(headers)+2))
        ws.append_row(headers, value_input_option="RAW")
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
    existing = ws.row_values(1)
    if existing != headers:
        ws.update('A1', [headers])
    return ws

def safe_pct_to_float(s):
    if not s:
        return ""
    return float(str(s).replace("%","").replace(",",".").strip())

def analyze_bsl(install_rows):
    target = [r for r in install_rows if r[1] == "Bas-Saint-Laurent"]
    values = [r[8] for r in target if isinstance(r[8], (int, float))]
    if len(values) < 2:
        return []
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    std = variance ** 0.5
    out = []
    for r in target:
        taux = r[8]
        z = 0 if std == 0 else (taux - mean) / std
        level = "2σ" if z >= 2 else ("1σ" if z >= 1 else "")
        if level:
            out.append([r[0], r[2], taux, mean, std, z, level, r[11]])
    return out

def main():
    if not should_run_now():
        logging.info("Heure locale non ciblée, aucune écriture.")
        return
    resp = requests.get(URL, timeout=60, headers={"User-Agent":"Mozilla/5.0"})
    resp.raise_for_status()
    lines = clean_lines(resp.text)
    global_data = parse_global(lines)
    region_blocks = find_region_blocks(lines)
    parsed_regions = [parse_region(region, block) for region, block in region_blocks.items()]
    current_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    local_now = now_local()
    local_date = local_now.strftime("%Y-%m-%d")
    local_time = local_now.strftime("%H:%M:%S")
    sh = auth_sheet()
    ws_global = ensure_worksheet(sh, "releves_global_quebec", ["horodatage_utc","date_locale","heure_locale","derniere_mise_a_jour_site","nb_total_urgence","nb_attente_medecin","duree_salle_attente","duree_attente_civiere","civieres_fonctionnelles","civieres_occupees","taux_occupation","patients_plus_24h","patients_plus_48h","source_url"])
    ws_regions = ensure_worksheet(sh, "releves_regions", ["horodatage_utc","region","date_locale","taux_region","source_url"])
    ws_install = ensure_worksheet(sh, "releves_installations", ["horodatage_utc","region","installation","date_locale","nb_total_urgence","nb_attente_medecin","civieres_fonctionnelles","civieres_occupees","taux_occupation","patients_plus_24h","patients_plus_48h","source_url"])
    ws_anom = ensure_worksheet(sh, "anomalies_bsl", ["horodatage_utc","installation","taux_occupation","moyenne_session","ecart_type_session","z_score","niveau","source_url"])
    ws_global.append_rows([[
        current_utc, local_date, local_time, global_data["derniere_mise_a_jour_site"],
        int(global_data["nb_total_urgence"]) if global_data["nb_total_urgence"] else "",
        int(global_data["nb_attente_medecin"]) if global_data["nb_attente_medecin"] else "",
        global_data["duree_salle_attente"], global_data["duree_attente_civiere"],
        int(global_data["civieres_fonctionnelles"]) if global_data["civieres_fonctionnelles"] else "",
        int(global_data["civieres_occupees"]) if global_data["civieres_occupees"] else "",
        safe_pct_to_float(global_data["taux_occupation"]),
        int(global_data["patients_plus_24h"]) if global_data["patients_plus_24h"] else "",
        int(global_data["patients_plus_48h"]) if global_data["patients_plus_48h"] else "",
        URL
    ]], value_input_option="USER_ENTERED")
    region_rows, install_rows = [], []
    for region in parsed_regions:
        region_rows.append([current_utc, region["region"], local_date, safe_pct_to_float(region["taux_region"]), URL])
        for inst in region["installations"]:
            install_rows.append([current_utc, region["region"], inst["installation"], local_date, inst["nb_total_urgence"], inst["nb_attente_medecin"], inst["civieres_fonctionnelles"], inst["civieres_occupees"], safe_pct_to_float(inst["taux_occupation"]), inst["patients_plus_24h"], inst["patients_plus_48h"], URL])
    if region_rows:
        ws_regions.append_rows(region_rows, value_input_option="USER_ENTERED")
    if install_rows:
        ws_install.append_rows(install_rows, value_input_option="USER_ENTERED")
    anomalies = analyze_bsl(install_rows)
    if anomalies:
        ws_anom.append_rows(anomalies, value_input_option="USER_ENTERED")
    logging.info("Terminé: %s régions, %s installations, %s anomalies", len(region_rows), len(install_rows), len(anomalies))

if __name__ == "__main__":
    main()
