# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore")

import os
import argparse
import csv
import math
import json
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO

import certifi
import requests
import yaml
from pymongo import MongoClient
import vertexai
from vertexai.generative_models import GenerativeModel

# ========== 1. Konfiguration & GCP / MongoDB Setup ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_config():
    config_path = os.path.join(BASE_DIR, "env.yaml")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Konfigurationsdatei '{config_path}' nicht gefunden.")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# Google Application Credentials setzen (zentral oder lokal)
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
    candidate_keys = [
        os.path.join(BASE_DIR, "vertex_key.json"),
        os.path.join(BASE_DIR, "credentials.json"),
        "/home/joris/Python_Programme/Wetter/credentials.json"
    ]
    for key_path in candidate_keys:
        if os.path.exists(key_path):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path
            break

# Vertex AI initialisieren
GCP_PROJECT = CONFIG.get("GCP_PROJECT_ID", "np360-477519")
GCP_LOCATION = CONFIG.get("GCP_LOCATION", "europe-west6")
vertexai.init(project=GCP_PROJECT, location=GCP_LOCATION)
AI_MODEL_NAME = CONFIG.get("GEMINI_MODEL", "gemini-2.5-flash")

# MongoDB initialisieren
MONGO_URI = CONFIG.get("MONGO_URI")
MONGO_DB_NAME = CONFIG.get("MONGO_DB_NAME", "weather_bots")
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where()) if MONGO_URI else None
db = mongo_client[MONGO_DB_NAME] if mongo_client else None
collection_pollen = db["pollen_live"] if db is not None else None
collection_pollen_verlauf = db["pollen_verlauf"] if db is not None else None

SEEN_FILE = os.path.join(BASE_DIR, "seen_pollen.json")
NORM_FILE = os.path.join(BASE_DIR, "pollen_normwerte_1991_2020.json")

# ========== 2. Zweisprachige Matrix & Schwellenwerte ==========
DICT = {
    "stations": {
        "pge": {"de": "Genf / Bassin Lémanique", "fr": "Genève / Bassin Lémanique"},
        "pls": {"de": "Lausanne / Waadt", "fr": "Lausanne / Vaud"},
        "pne": {"de": "Neuenburg / Freiburg", "fr": "Neuchâtel / Fribourg"},
        "pbs": {"de": "Basel / Nordwestschweiz", "fr": "Bâle / Nord-ouest de la Suisse"},
        "psn": {"de": "Sitten / Wallis", "fr": "Sion / Valais"},
        "pbe": {"de": "Bern / Mittelland", "fr": "Berne / Plateau"},
        "plu": {"de": "Luzern / Innerschweiz", "fr": "Lucerne / Suisse centrale"},
        "pmu": {"de": "Aargau / Region Sempachersee", "fr": "Argovie / Région du lac de Sempach"},
        "pzh": {"de": "Zürich", "fr": "Zurich"},
        "pds": {"de": "Davos / Graubünden", "fr": "Davos / Grisons"},
        "plo": {"de": "Locarno / Tessin", "fr": "Locarno / Tessin"},
        "pbu": {"de": "St. Gallen / Ostschweiz", "fr": "Saint-Gall / Suisse orientale"}
    },
    "pollen": {
        "kabetud0": {"de": "Birke", "fr": "Bouleau", "url_name": "birke"},
        "khpoacd0": {"de": "Gräser", "fr": "Graminées", "url_name": "graeser"},
        "kafraxd0": {"de": "Esche", "fr": "Frêne", "url_name": "esche"},
        "kacoryd0": {"de": "Hasel", "fr": "Noisetier", "url_name": "hasel"},
        "kaalnud0": {"de": "Erle", "fr": "Aulne", "url_name": "erle"},
        "kaquerd0": {"de": "Eiche", "fr": "Chêne", "url_name": "eiche"},
        "kafagud0": {"de": "Buche", "fr": "Hêtre", "url_name": "buche"}
    },
    "wochentage": {
        "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
        "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    },
    "trends": {
        "gestiegen": {"de": "gestiegen", "fr": "en hausse"},
        "gesunken": {"de": "gesunken", "fr": "en baisse"},
        "gleichbleibend": {"de": "gleichbleibend", "fr": "stable"}
    },
    "aha_urls": {
        "de": "https://www.pollenundallergie.ch/polleninformationen/pollenprognosen",
        "fr": "https://www.pollenundallergie.ch/informations-polliniques/previsions-polliniques"
    }
}

THRESHOLDS = {
    "kabetud0": {"schwach": "1-10", "maessig": "11-69", "stark": 70, "sehr_stark": 300},
    "khpoacd0": {"schwach": "1-19", "maessig": "20-49", "stark": 50, "sehr_stark": 150},
    "kafraxd0": {"schwach": "1-10", "maessig": "11-99", "stark": 100, "sehr_stark": 350},
    "kacoryd0": {"schwach": "1-10", "maessig": "11-69", "stark": 70, "sehr_stark": 250},
    "kaalnud0": {"schwach": "1-10", "maessig": "11-69", "stark": 70, "sehr_stark": 250},
    "kaquerd0": {"schwach": "1-49", "maessig": "50-129", "stark": 130, "sehr_stark": 400},
    "kafagud0": {"schwach": "1-49", "maessig": "50-129", "stark": 130, "sehr_stark": 400}
}

# ========== 3. Hilfsfunktionen & Normwerte ==========
def _load_json(path: str, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

NORMWERTE = _load_json(NORM_FILE, {})

# ========== 4. Daten-Beschaffung ==========
def fetch_current_background_text(lang: str) -> str:
    url = DICT["aha_urls"][lang]
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', response.text, flags=re.IGNORECASE | re.DOTALL)
        clean = [re.sub(r'<[^>]+>', '', p).strip() for p in paragraphs]
        editorial = [p for p in clean if len(p) > 80 and "Cookie" not in p and "Copyright" not in p]
        if editorial:
            return "\n\n".join(editorial)[:1500]
    except Exception:
        pass
    return ""

def parse_messzeit(row: dict):
    """Offizieller reference_timestamp ist UTC, meist dd.mm.yyyy HH:MM.

    Kein Rückfall aufs Abrufdatum: Ohne prüfbaren Zeitstempel kein Datensatz.
    """
    raw = (row.get("reference_timestamp") or row.get("ReferenceTS") or "").strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_pollenwert(raw):
    """0 ist ein Messwert; Leerfeld, Fehlcode und negative Werte sind KEINE 0."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        wert = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return wert if math.isfinite(wert) and wert >= 0 else None


def hat_messwerte(row: dict) -> bool:
    return any(parse_pollenwert(row.get(p)) is not None for p in THRESHOLDS)


def select_latest_rows(rows: list):
    """Letzter gültiger Messtag und der letzte VORTAG mit Messdaten.

    Liefert ({}, {}) statt ein unbekanntes Datum oder leere Werte zu erfinden.
    """
    heute_utc = datetime.now(timezone.utc)
    valid = []
    for row in rows:
        stamp = parse_messzeit(row)
        if stamp is not None and stamp <= heute_utc + timedelta(days=1) and hat_messwerte(row):
            valid.append((stamp, row))
    if not valid:
        return {}, {}
    valid.sort(key=lambda item: item[0])
    aktuell_zeit, aktuell = valid[-1]
    vorher = next((row for ts, row in reversed(valid[:-1])
                   if ts.date() < aktuell_zeit.date()), {})
    return aktuell, vorher


def fetch_latest_csv_data(station_id: str):
    headers = {"User-Agent": "Mozilla/5.0"}
    url_sub = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/{station_id}/ogd-pollen_{station_id}_d_recent.csv"
    url_root = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/ogd-pollen_{station_id}_d_recent.csv"

    for url in (url_sub, url_root):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            reader = csv.DictReader(StringIO(r.content.decode("utf-8-sig", errors="replace")), delimiter=";")
            if not reader.fieldnames or "reference_timestamp" not in reader.fieldnames:
                continue
            if not any(p in reader.fieldnames for p in THRESHOLDS):
                continue
            aktuell, vorher = select_latest_rows(list(reader))
            if aktuell:
                return aktuell, vorher
        except (requests.RequestException, UnicodeError, csv.Error):
            continue
    return {}, {}

def get_historical_context(station_id: str, param_id: str, threshold: float, current_val: float, lang: str) -> str:
    station_data = NORMWERTE.get(station_id, {})
    param_data = station_data.get(param_id, {})
    if not param_data:
        return "Für diese Region/Pollenart liegen keine ausreichenden Klimanormwerte (1991-2020) vor." if lang == "de" else "Données climatiques (1991-2020) insuffisantes pour cette région/ce pollen."

    current_year = datetime.now().year
    today_yday = datetime.now().timetuple().tm_yday
    avg_start_yday = param_data.get("avg_start_yday")
    avg_heute = param_data.get("daily_normals", {}).get(str(today_yday), 0.0)

    kontext = []
    if avg_start_yday:
        avg_date = datetime(current_year, 1, 1) + timedelta(days=avg_start_yday - 1)
        diff = today_yday - avg_start_yday
        if diff < -7:
            kontext.append(f"Der Saisonstart erfolgte extrem früh (Schnitt 1991–2020: um den {avg_date.strftime('%d.%m.')})." if lang == "de" else f"Le début de saison a été très précoce (Moyenne 1991-2020: vers le {avg_date.strftime('%d.%m.')}).")

    if avg_heute > 0:
        if current_val > (avg_heute * 1.5):
            kontext.append(f"Aktuelle Belastung liegt massiv über dem langjährigen Schnitt (Klimanorm: {avg_heute:.1f} P/m³)." if lang == "de" else f"La charge actuelle est massivement supérieure à la moyenne à long terme (Norme climatique: {avg_heute:.1f} P/m³).")
        elif current_val < (avg_heute * 0.5):
            kontext.append(f"Aktuelle Belastung tiefer als langjähriger Schnitt (Klimanorm: {avg_heute:.1f} P/m³)." if lang == "de" else f"La charge actuelle est inférieure à la moyenne à long terme (Norme climatique: {avg_heute:.1f} P/m³).")

    return " ".join(kontext) if kontext else ("Kein Vergleich möglich." if lang == "de" else "Aucune comparaison possible.")

# ========== 5. Vertex AI Integration ==========
def generate_sda_pollen_article(lang: str, region: str, triggering_alerts: list, other_alerts: list,
                                current_context: str, date_str: str, messdatum_str: str, wochentag: str) -> str:
    if lang == "de":
        alert_text = "=== AUSLÖSER DER MELDUNG (Massiver Anstieg / Neu) ===\n" + "\n".join(triggering_alerts)
        if other_alerts:
            alert_text += "\n\n=== EBENFALLS AKTUELL STARK ===\n" + "\n".join(other_alerts)

        system_instruction = """
        Du bist Redaktor für Keystone-SDA. Verfasse eine Agenturmeldung (150-250 Wörter).
        1. TITEL: "Gesundheit - Starke Pollenbelastung in [Region] (TT.MM.JJJJ)" (Datum=Publikationsdatum).
        2. LEAD: "In [Region] hat die Belastung durch [Pollen] die [zweithöchste/höchste] Stufe '[stark/sehr stark]' erreicht. Mit einem Tagesdurchschnitt von [X] Pollen pro Kubikmeter ist sie am [Messdatum] im Vergleich zum Vortag ([Y]) massiv gestiegen, wie MeteoSchweiz am [Wochentag] zeigte." (Bei weiteren Pollen anfügen: "Zudem verharren auch [Pollen] auf starkem Niveau.")
        3. SKALA & HISTORIE: Übernimm die mitgelieferte Skalendefinition exakt. Erkläre den historischen Saisonstart und Intensität für die Region.
        4. HINTERGRUND: Nutze den Bericht von aha!. Nur Indikativ.
        """
    else:
        alert_text = "=== DÉCLENCHEURS DE L'ALERTE (Forte hausse / Nouveau) ===\n" + "\n".join(triggering_alerts)
        if other_alerts:
            alert_text += "\n\n=== ÉGALEMENT FORT ACTUELLEMENT ===\n" + "\n".join(other_alerts)

        system_instruction = """
        Tu es rédacteur pour l'agence de presse Keystone-ATS. Rédige le texte d'une dépêche (150-250 mots).
        RÈGLES DE STYLE STRICTES :
        - N'écris AUCUN titre. N'écris PAS d'amorce géographique ou d'agence.
        - Fais des phrases courtes et percutantes. Divise les phrases longues.
        - Utilise des guillemets classiques ("fort") et le terme "moyenne quotidienne".
        STRUCTURE IMPOSÉE :
        1. LEAD : "La concentration de pollens de [Pollens] a atteint le niveau "[fort/très fort]" dans la région [Région], selon les données de MétéoSuisse publiées [Jour de la semaine]. Avec une moyenne quotidienne de [X] pollens par mètre cube, elle a massivement augmenté par rapport à la veille ([Y])."
        2. ÉCHELLE & HISTORIQUE : Explique l'échelle et résume l'historique en phrases courtes.
        3. CONTEXTE : Résume les informations de "aha! Centre d'Allergie" de manière factuelle.
        """

    prompt = f"Publikationsdatum/Date: {date_str}\nMessdatum: {messdatum_str}\nWochentag/Jour: {wochentag}\nRegion: {region}\n\nDATEN/DONNÉES:\n{alert_text}\n\nHINTERGRUND/CONTEXTE:\n{current_context}"

    try:
        model = GenerativeModel(AI_MODEL_NAME, system_instruction=[system_instruction])
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"Vertex-AI-Fehler: {e}"

# ========== 6. E-Mail Versand ==========
def send_email(subject: str, body: str, to_key: str):
    from_addr = CONFIG.get("MAIL_FROM_DE") or CONFIG.get("SMTP_USER")
    to_addr = CONFIG.get(to_key)

    if not from_addr or not to_addr:
        print(f"⚠️ Mail übersprungen: Absender oder Empfänger ({to_key}) nicht konfiguriert.")
        return

    if isinstance(to_addr, list):
        recipients = to_addr
        to_header = ", ".join(to_addr)
    else:
        recipients = [addr.strip() for addr in str(to_addr).split(",") if addr.strip()]
        to_header = str(to_addr)

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_header
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        server = smtplib.SMTP(CONFIG["SMTP_SERVER"], int(CONFIG.get("SMTP_PORT", 587)))
        if CONFIG.get("SMTP_STARTTLS", True):
            server.starttls()
        server.login(CONFIG["SMTP_USER"], CONFIG["SMTP_PASSWORD"])
        server.sendmail(from_addr, recipients, msg.as_string())
        server.quit()
        print(f"🚨 Mail erfolgreich gesendet an {recipients}: {subject}")
    except Exception as e:
        print(f"❌ Mail-Fehler bei {to_key}: {e}")

# ========== 7. MongoDB Synchronisation ==========
def baue_pollen_dokument(station_id: str, row_data: dict):
    """Bestehende pollen_live-Felder bleiben kompatibel mit dem Gemeinde-Widget."""
    messtag = parse_messzeit(row_data)
    if messtag is None or not hat_messwerte(row_data):
        raise ValueError("Kein gültiger Messzeitstempel oder keine gültigen Pollenwerte")

    pollen_values = {}
    highest_level = None
    rang = {"schwach": 1, "maessig": 2, "stark": 3, "sehr stark": 4}
    for param, rules in THRESHOLDS.items():
        val_float = parse_pollenwert(row_data.get(param))
        p_info = DICT["pollen"].get(param, {})
        level = None
        if val_float is not None:
            if val_float >= rules["sehr_stark"]:
                level = "sehr stark"
            elif val_float >= rules["stark"]:
                level = "stark"
            elif val_float >= int(rules["maessig"].split("-")[0]):
                level = "maessig"
            else:
                level = "schwach"
            if highest_level is None or rang[level] > rang[highest_level]:
                highest_level = level
        pollen_values[param] = {
            "name_de": p_info.get("de"), "name_fr": p_info.get("fr"),
            "value": val_float, "level": level
        }

    return {
        "station_id": station_id,
        "name_de": DICT["stations"][station_id]["de"],
        "name_fr": DICT["stations"][station_id]["fr"],
        "messdatum": messtag.strftime("%d.%m.%Y"),
        "messdatum_iso": messtag.date().isoformat(),
        "messzeit_utc": messtag,
        "highest_level": highest_level,
        "values": pollen_values,
        "datenqualitaet": {
            "gueltige_pollenarten": sum(v["value"] is not None for v in pollen_values.values()),
            "fehlende_pollenarten": [p for p, v in pollen_values.items() if v["value"] is None]
        },
        "quelle": "MeteoSchweiz OGD Pollen, Tagesmittel d_recent"
    }


def sync_mongodb_station(station_id: str, row_data: dict, messdatum_str: str):
    """Speichert aktuellen Tageswert und Verlauf; ändert keine Meldungslogik.

    Ist ein Datum bereits vorhanden und unverändert, erfolgen keine Schreibzugriffe.
    Ein alter Datensatz darf einen neueren Live-Datensatz nicht überschreiben.
    """
    if collection_pollen is None or collection_pollen_verlauf is None:
        raise RuntimeError("MongoDB pollen_live/pollen_verlauf nicht konfiguriert")
    doc = baue_pollen_dokument(station_id, row_data)
    messtag_iso = doc["messdatum_iso"]
    now = datetime.now(timezone.utc)
    history_id = f"{station_id}:{messtag_iso}"
    existing_history = collection_pollen_verlauf.find_one({"_id": history_id})
    felder = ("messdatum_iso", "values", "highest_level", "datenqualitaet")
    if not existing_history or any(existing_history.get(k) != doc.get(k) for k in felder):
        collection_pollen_verlauf.update_one(
            {"_id": history_id},
            {"$set": {**doc, "last_updated": now}, "$setOnInsert": {"created_at": now}},
            upsert=True
        )

    existing = collection_pollen.find_one({"station_id": station_id})
    previous_iso = (existing or {}).get("messdatum_iso")
    if previous_iso and previous_iso > messtag_iso:
        return "aelter_als_live"
    if existing and all(existing.get(k) == doc.get(k) for k in felder):
        return "unveraendert"
    collection_pollen.update_one(
        {"station_id": station_id},
        {"$set": {**doc, "last_updated": now}},
        upsert=True
    )
    return "aktualisiert"

# ========== 8. Hauptlauf ==========
def process_pollen(dry_run=False, nur_daten=False):
    seen = _load_json(SEEN_FILE, {})
    today_str = datetime.now().strftime("%d.%m.%Y")
    seen_date_str = datetime.now().strftime("%Y-%m-%d")
    weekday_idx = datetime.now().weekday()

    # Im Test- oder reinen Datenbetrieb keine zusätzlichen Webseiten abrufen.
    bg_texts = ({"de": "", "fr": ""} if dry_run or nur_daten else {
        "de": fetch_current_background_text("de"),
        "fr": fetch_current_background_text("fr")
    })

    print(f"🔄 Starte Abfrage der Pollendaten für alle Stationen ({today_str})...")
    updated_stations = 0

    for station_id, s_info in DICT["stations"].items():
        st_name = s_info["de"]
        print(f"📡 Station {station_id.upper()} ({st_name}):")

        csv_today, csv_last = fetch_latest_csv_data(station_id)
        if not csv_today:
            print("   ⚠️ Keine CSV-Daten verfügbar (MeteoSchweiz liefert für diese Station aktuell keine Daten).")
            continue

        messzeit = parse_messzeit(csv_today)
        if messzeit is None or not hat_messwerte(csv_today):
            print("   ⚠️ Ungültige Quelldaten: kein MongoDB-Schreibzugriff, kein Alarm.")
            continue
        messdatum_str = messzeit.strftime("%d.%m.%Y")
        vorhanden = sum(parse_pollenwert(csv_today.get(p)) is not None for p in THRESHOLDS)
        fehlend = len(THRESHOLDS) - vorhanden

        if dry_run:
            print(f"   🧪 TROCKENLAUF: Messdatum {messdatum_str}; gültige Werte {vorhanden}/7, fehlend {fehlend}. Keine Speicherung, keine Mails.")
            updated_stations += 1
            continue

        # 1. MongoDB LIVE-HUB UND TAGESVERLAUF IMMER SYNCHRONISIEREN
        status = sync_mongodb_station(station_id, csv_today, messdatum_str)
        updated_stations += 1
        print(f"   💾 MongoDB pollen_live / pollen_verlauf: {status} (Messdatum: {messdatum_str}; gültige Werte {vorhanden}/7).")

        if nur_daten:
            print("   ℹ️ Nur-Daten-Modus: Keine Alarmprüfung, keine Mails.")
            continue
        if status == "aelter_als_live":
            print("   ℹ️ Quellmessung älter als aktueller MongoDB-Stand: kein Alarm.")
            continue
        if datetime.now(timezone.utc) - messzeit > timedelta(days=3):
            print("   ℹ️ Messdaten älter als drei Tage: kein Agenturalarm.")
            continue
        if seen.get(f"{station_id}_checked_messdatum") == messdatum_str:
            print("   ℹ️ Dieser Messtag wurde bereits redaktionell geprüft.")
            continue

        # 2. ALARM-PRÜFUNG (Wird nur 1x täglich pro Station ausgelöst)
        if seen.get(f"{station_id}_run_date") == seen_date_str:
            print("   ℹ️ Alarm-Check für heute bereits abgeschlossen.")
            continue

        alerts = {"de": {"trigger": [], "other": [], "links": [], "desk": []},
                  "fr": {"trigger": [], "other": [], "links": [], "desk": []}}
        has_trigger = False

        for param, rules in THRESHOLDS.items():
            val_str = csv_today.get(param, "")
            if not val_str:
                continue
            try:
                val_float = float(val_str)
                if val_float >= rules["stark"]:
                    p_info = DICT["pollen"][param]
                    prev_val = parse_pollenwert(csv_last.get(param))
                    # Ohne belastbaren Vortagswert keine prozentuale Trendbehauptung.
                    if prev_val is None:
                        continue
                    prev_float = prev_val

                    if val_float > prev_float * 1.1:
                        trend_key = "gestiegen"
                    elif val_float < prev_float * 0.9:
                        trend_key = "gesunken"
                    else:
                        trend_key = "gleichbleibend"

                    last_val = seen.get(f"{station_id}_{param}", {}).get("value", 0.0)
                    is_trigger = (trend_key == "gestiegen" and (last_val == 0.0 or val_float >= last_val * 1.5))
                    if is_trigger:
                        has_trigger = True

                    for lang in ["de", "fr"]:
                        name = p_info[lang]
                        trend_txt = DICT["trends"][trend_key][lang]
                        hist_txt = get_historical_context(station_id, param, rules["stark"], val_float, lang)

                        if val_float >= rules["sehr_stark"]:
                            skala = "Die höchste Stufe 'sehr stark' bedeutet extremes Risiko für Allergiker." if lang == "de" else "Le niveau 'très fort' signifie un risque extrême pour les personnes allergiques."
                        else:
                            skala = "Die Stufe 'stark' bedeutet deutliche Symptome bei den meisten Betroffenen." if lang == "de" else "Le niveau 'fort' signifie des symptômes marqués chez la plupart des personnes allergiques."

                        info_txt = f"{name} ({val_float} P/m³, Vortag: {prev_float}, Trend: {trend_txt}. Historie: {hist_txt} Skala: {skala})"

                        if is_trigger:
                            alerts[lang]["trigger"].append(info_txt)
                        else:
                            alerts[lang]["other"].append(info_txt)

                        st_lang_name = DICT["stations"][station_id][lang]
                        if lang == "de":
                            url = f"https://www.meteoschweiz.admin.ch/service-und-publikationen/applikationen/messwerte.html#param=messwerte-pollen-{p_info['url_name']}-1h&station={station_id.upper()}&table=false"
                            alerts["de"]["links"].append(f"- Verifikation {name} in {st_lang_name}: {url}")
                            alerts["de"]["desk"].append(f'- {name}: "schwach" ({rules["schwach"]}), "mässig" ({rules["maessig"]}), "stark" ({rules["stark"]}-{rules["sehr_stark"] - 1}), "sehr stark" (ab {rules["sehr_stark"]}).')
                        else:
                            url = f"https://www.meteosuisse.admin.ch/services-et-publications/applications/valeurs-mesurees.html#param=messwerte-pollen-{p_info['url_name']}-1h&station={station_id.upper()}&table=false"
                            alerts["fr"]["links"].append(f"- Vérification {name} à {st_lang_name}: {url}")
                            alerts["fr"]["desk"].append(f'- {name}: "faible" ({rules["schwach"]}), "modéré" ({rules["maessig"]}), "fort" ({rules["stark"]}-{rules["sehr_stark"] - 1}), "très fort" (dès {rules["sehr_stark"]}).')

            except ValueError:
                pass

        if has_trigger:
            print(f"🚨 Auslöser erkannt für Station {station_id.upper()}! Generiere Textmeldungen...")
            for lang, to_key in [("de", "MAIL_TO_DE"), ("fr", "MAIL_TO_FR")]:
                if not alerts[lang]["trigger"]:
                    continue

                reg_name = DICT["stations"][station_id][lang]
                wochentag = DICT["wochentage"][lang][weekday_idx]

                article = generate_sda_pollen_article(
                    lang, reg_name, alerts[lang]["trigger"], alerts[lang]["other"],
                    bg_texts[lang], today_str, messdatum_str, wochentag
                )

                subj = article.split("\n")[0] if article and lang == "de" else f"Pollen: {reg_name}"
                if lang == "fr":
                    subj = f"Alerte Pollens: {reg_name} ({today_str})"

                if lang == "de":
                    notes = "\n\n---\nQUELLEN ZUR VERIFIKATION:\n" + "\n".join(alerts["de"]["links"]) + f"\n\nHintergrund: {DICT['aha_urls']['de']}\n\nSchwellenwerte:\n" + "\n".join(alerts["de"]["desk"])
                    body = f"{article}\n\n---\nTrigger:\n" + "\n".join(alerts["de"]["trigger"]) + notes
                else:
                    notes = "\n\n---\nSOURCES POUR VÉRIFICATION:\n" + "\n".join(alerts["fr"]["links"]) + f"\n\nContexte: {DICT['aha_urls']['fr']}\n\nSeuils:\n" + "\n".join(alerts["fr"]["desk"])
                    body = f"{article}\n\n---\nDéclencheurs:\n" + "\n".join(alerts["fr"]["trigger"]) + notes

                send_email(subj, body, to_key)

            for p, r in THRESHOLDS.items():
                v = csv_today.get(p, "")
                if v and float(v) >= r["stark"]:
                    seen[f"{station_id}_{p}"] = {"date": today_str, "value": float(v)}

        for p, r in THRESHOLDS.items():
            v = csv_today.get(p, "")
            if v:
                try:
                    if float(v) < r["stark"] and f"{station_id}_{p}" in seen:
                        seen[f"{station_id}_{p}"] = {"date": today_str, "value": 0.0}
                except ValueError:
                    pass

        seen[f"{station_id}_run_date"] = seen_date_str
        seen[f"{station_id}_checked_messdatum"] = messdatum_str
        _save_json(SEEN_FILE, seen)

    print(f"💾 Gesamt: {updated_stations} Stationen verarbeitet (pollen_live + pollen_verlauf; Testläufe schreiben nicht).")
    print("🏁 Pollen-Durchlauf vollständig abgeschlossen.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Einziger MeteoSchweiz-Pollenbot: MongoDB und Agenturmeldungen")
    modus = parser.add_mutually_exclusive_group()
    modus.add_argument("--dry-run", action="store_true",
                       help="Pollendaten abrufen und prüfen, ohne MongoDB-Schreibzugriffe oder E-Mails")
    modus.add_argument("--nur-daten", action="store_true",
                       help="MongoDB aktualisieren, aber keine KI-Generierung und keine E-Mails")
    args = parser.parse_args()
    process_pollen(dry_run=args.dry_run, nur_daten=args.nur_daten)