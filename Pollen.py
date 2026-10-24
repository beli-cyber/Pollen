# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore")

import os
import csv
import json
import re
import smtplib
from datetime import datetime, timedelta
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

def fetch_latest_csv_data(station_id: str):
    headers = {"User-Agent": "Mozilla/5.0"}
    url_sub = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/{station_id}/ogd-pollen_{station_id}_d_recent.csv"
    url_root = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/ogd-pollen_{station_id}_d_recent.csv"

    for url in [url_sub, url_root]:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                reader = csv.DictReader(StringIO(r.text), delimiter=';')
                rows = list(reader)
                if not rows:
                    continue

                valid_row = None
                valid_prev_row = None
                for i in range(len(rows) - 1, -1, -1):
                    if any(rows[i].get(p, "").strip() != "" for p in THRESHOLDS.keys()):
                        valid_row = rows[i]
                        valid_prev_row = rows[i - 1] if i > 0 else rows[i]
                        break

                if not valid_row:
                    valid_row = rows[-1]
                    valid_prev_row = rows[-2] if len(rows) >= 2 else rows[0]

                return valid_row, valid_prev_row
        except Exception:
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
def sync_mongodb_station(station_id: str, row_data: dict, messdatum_str: str):
    if collection_pollen is None:
        return

    pollen_values = {}
    highest_level = "schwach"

    for param, rules in THRESHOLDS.items():
        val_str = row_data.get(param, "")
        try:
            val_float = float(val_str) if val_str else 0.0
        except ValueError:
            val_float = 0.0

        p_info = DICT["pollen"].get(param, {})
        level = "schwach"
        if val_float >= rules["sehr_stark"]:
            level = "sehr stark"
            highest_level = "sehr stark"
        elif val_float >= rules["stark"]:
            level = "stark"
            if highest_level != "sehr stark":
                highest_level = "stark"
        elif val_float >= 10:
            level = "maessig"

        pollen_values[param] = {
            "name_de": p_info.get("de"),
            "name_fr": p_info.get("fr"),
            "value": val_float,
            "level": level
        }

    doc = {
        "station_id": station_id,
        "name_de": DICT["stations"][station_id]["de"],
        "name_fr": DICT["stations"][station_id]["fr"],
        "messdatum": messdatum_str,
        "highest_level": highest_level,
        "values": pollen_values,
        "last_updated": datetime.utcnow()
    }

    collection_pollen.update_one(
        {"station_id": station_id},
        {"$set": doc},
        upsert=True
    )

# ========== 8. Hauptlauf ==========
def process_pollen():
    seen = _load_json(SEEN_FILE, {})
    today_str = datetime.now().strftime("%d.%m.%Y")
    seen_date_str = datetime.now().strftime("%Y-%m-%d")
    weekday_idx = datetime.now().weekday()

    bg_texts = {
        "de": fetch_current_background_text("de"),
        "fr": fetch_current_background_text("fr")
    }

    print(f"🔄 Starte Abfrage der Pollendaten für alle Stationen ({today_str})...")
    updated_stations = 0

    for station_id, s_info in DICT["stations"].items():
        st_name = s_info["de"]
        print(f"📡 Station {station_id.upper()} ({st_name}):")

        csv_today, csv_last = fetch_latest_csv_data(station_id)
        if not csv_today:
            print("   ⚠️ Keine CSV-Daten verfügbar (MeteoSchweiz liefert für diese Station aktuell keine Daten).")
            continue

        messdatum_str = today_str
        for key, val in csv_today.items():
            if key and any(k in key.lower() for k in ["date", "datum", "time"]):
                try:
                    messdatum_str = datetime.strptime(val[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
                except ValueError:
                    pass
                break

        # 1. MongoDB LIVE-HUB IMMER AKTUALISIEREN
        sync_mongodb_station(station_id, csv_today, messdatum_str)
        updated_stations += 1
        print(f"   💾 In MongoDB ('pollen_live') aktualisiert (Messdatum: {messdatum_str}).")

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
                    prev_float = float(csv_last.get(param, "0"))

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
        _save_json(SEEN_FILE, seen)

    print(f"💾 Gesamt: {updated_stations} Stationen in MongoDB ('pollen_live') aktualisiert.")
    print("🏁 Pollen-Durchlauf vollständig abgeschlossen.")

if __name__ == "__main__":
    process_pollen()