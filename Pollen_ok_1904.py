# -*- coding: utf-8 -*-
"""
PollenSDA -> Web-Scraping & CSV -> Gemini KI -> SDA-Meldung -> E-Mail
- Steuerung via config.json
- Zieht OGD-Pollen-CSV des Bundes
- Berechnet Trends zum Vortag und Historie
- Trennt Auslöser von Mitschwimmern
- KI generiert strikt strukturierte Meldung
"""
import os
import json
import smtplib
import requests
import html
import re
import csv
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO

from google import genai
from google.genai import types


# ========== 1. Konfiguration laden ==========
def load_config():
    config_path = "config.json"
    if not os.path.exists(config_path):
        print(f"Fehler: {config_path} nicht gefunden!")
        exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
GEMINI_API_KEY = CONFIG.get("GEMINI_API_KEY", "")
GEMINI_MODEL = CONFIG.get("GEMINI_MODEL", "gemini-2.5-flash")
EMAIL_SENDER = CONFIG.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = CONFIG.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = CONFIG.get("EMAIL_RECEIVER", "")

SEEN_FILE = "seen_pollen.json"

# ========== 2. Definitionen ==========
STATIONS = {
    "pge": "Genf / Bassin Lémanique",
    "pls": "Lausanne / Waadt",
    "pne": "Neuenburg / Freiburg",
    "pbs": "Basel / Nordwestschweiz",
    "psn": "Sitten / Wallis",
    "pbe": "Bern / Mittelland",
    "plu": "Luzern / Innerschweiz",
    "pmu": "Aargau / Region Sempachersee",
    "pzh": "Zürich",
    "pds": "Davos / Graubünden",
    "plo": "Locarno / Tessin",
    "pbu": "St. Gallen / Ostschweiz"
}

# 100% ABGEGLICHEN MIT DEM OFFIZIELLEN METEOSCHWEIZ PDF
THRESHOLDS = {
    "kabetud0": {"name": "Birke", "schwach": "1-10", "maessig": "11-69", "stark": 70, "sehr_stark": 300},
    "khpoacd0": {"name": "Gräser", "schwach": "1-19", "maessig": "20-49", "stark": 50, "sehr_stark": 150},
    "kafraxd0": {"name": "Esche", "schwach": "1-10", "maessig": "11-99", "stark": 100, "sehr_stark": 350},
    "kacoryd0": {"name": "Hasel", "schwach": "1-10", "maessig": "11-69", "stark": 70, "sehr_stark": 250},
    "kaalnud0": {"name": "Erle", "schwach": "1-10", "maessig": "11-69", "stark": 70, "sehr_stark": 250},
    "kquercd0": {"name": "Eiche", "schwach": "1-49", "maessig": "50-129", "stark": 130, "sehr_stark": 400},
    "kfagusd0": {"name": "Buche", "schwach": "1-49", "maessig": "50-129", "stark": 130, "sehr_stark": 400},
    "kplatad0": {"name": "Platane", "schwach": "1-49", "maessig": "50-99", "stark": 100, "sehr_stark": 400},
    "kcarpid0": {"name": "Hagebuche", "schwach": "1-10", "maessig": "11-69", "stark": 70, "sehr_stark": 250}
}


# ========== 3. Hilfsfunktionen ==========
def _load_json(path: str, fallback):
    if not os.path.exists(path): return fallback
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ========== 4. Daten-Beschaffung ==========
def fetch_current_background_text() -> str:
    url = "https://www.pollenundallergie.ch/polleninformationen/pollenprognosen"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        html_content = response.text
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_content, flags=re.IGNORECASE | re.DOTALL)
        clean_text_blocks = [re.sub(r'<[^>]+>', '', p).strip() for p in paragraphs]
        editorial_paragraphs = [p for p in clean_text_blocks if
                                len(p) > 80 and "Cookie" not in p and "Copyright" not in p]
        if editorial_paragraphs:
            return "\n\n".join(editorial_paragraphs)[:1500]
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
                if rows:
                    # [-1] ist heute (aktuellste Messung), [-2] ist gestern (Vortag)
                    return rows[-1], (rows[-2] if len(rows) >= 2 else rows[0])
        except Exception:
            continue
    return {}, {}


def get_historical_context(station_id: str, param_id: str, threshold: float, current_val: float) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    url_sub = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/{station_id}/ogd-pollen_{station_id}_d_historical.csv"
    url_root = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/ogd-pollen_{station_id}_d_historical.csv"

    csv_text = ""
    for url in [url_sub, url_root]:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.encoding = 'utf-8'
            if r.status_code == 200:
                csv_text = r.text
                break
        except:
            continue

    if not csv_text: return "Keine historischen Daten verfügbar."

    lines = csv_text.splitlines()
    if not lines: return "Die historische Datei ist leer."

    header_idx, delimiter = -1, ';'
    for i, line in enumerate(lines[:20]):
        line_clean = line.replace('\ufeff', '').lower()
        if param_id.lower() in line_clean:
            header_idx = i
            delimiter = ';' if ';' in line_clean else ','
            break

    if header_idx == -1: return "Kopfzeile nicht gefunden."

    headers_list = [h.strip().replace('"', '').replace("'", "") for h in
                    lines[header_idx].replace('\ufeff', '').lower().split(delimiter)]

    date_idx = -1
    for idx, col_name in enumerate(headers_list):
        if any(keyword in col_name for keyword in ["date", "datum", "time", "termin", "tag"]):
            date_idx = idx
            break
    if date_idx == -1: date_idx = 0

    try:
        param_idx = headers_list.index(param_id.lower())
    except ValueError:
        return "Pollen-Spalte nicht gefunden."

    current_year = datetime.now().year
    today_yday = datetime.now().timetuple().tm_yday

    jahres_daten = {}
    heute_historisch = []
    min_year, max_year = 9999, 0

    for line in lines[header_idx + 1:]:
        row = line.split(delimiter)
        if len(row) <= max(date_idx, param_idx): continue

        date_str, val_str = row[date_idx].strip().replace('"', ''), row[param_idx].strip().replace('"', '')
        if date_str and val_str and val_str.lower() not in ("nan", "-", "", "na", "null"):
            try:
                date_str_clean = date_str[:10]
                try:
                    date_obj = datetime.strptime(date_str_clean, "%Y-%m-%d")
                except ValueError:
                    try:
                        date_obj = datetime.strptime(date_str_clean, "%d.%m.%Y")
                    except ValueError:
                        continue

                val_float = float(val_str.replace(',', '.'))
                year, row_yday = date_obj.year, date_obj.timetuple().tm_yday

                if year == current_year: continue
                if year < min_year: min_year = year
                if year > max_year: max_year = year

                if val_float >= threshold:
                    if year not in jahres_daten: jahres_daten[year] = row_yday
                if row_yday == today_yday: heute_historisch.append(val_float)
            except ValueError:
                pass

    kontext_texte = []
    zeitraum_text = f"der Jahre {min_year} bis {max_year}" if min_year != 9999 else "der Vorjahre"

    if jahres_daten and min_year != 9999:
        avg_yday = int(sum(jahres_daten.values()) / len(jahres_daten))
        avg_date = datetime(current_year, 1, 1) + timedelta(days=avg_yday - 1)
        diff = today_yday - avg_yday
        if diff < -7:
            kontext_texte.append(
                f"Saisonstart in der Region ist extrem früh (im regionalen Schnitt {zeitraum_text} passierte das erst um den {avg_date.strftime('%d.%m.')}).")
        elif diff > 7:
            kontext_texte.append(
                f"Saisonstart in der Region ist eher spät (im regionalen Schnitt {zeitraum_text} passierte das schon um den {avg_date.strftime('%d.%m.')}).")
        else:
            kontext_texte.append(
                f"Saisonstart in der Region ist typisch (regionaler Schnitt {zeitraum_text} ist der {avg_date.strftime('%d.%m.')}).")

    if heute_historisch:
        avg_heute = sum(heute_historisch) / len(heute_historisch)
        if current_val > (avg_heute * 1.5) and current_val > 10:
            kontext_texte.append(
                f"Die aktuelle Belastung liegt massiv über dem langjährigen Durchschnitt der Region für diese Jahreszeit (Schnitt: {avg_heute:.1f}).")
        elif current_val < (avg_heute * 0.5):
            kontext_texte.append(
                f"Die aktuelle Belastung ist derzeit tiefer als im langjährigen Durchschnitt der Region für diese Jahreszeit (Schnitt: {avg_heute:.1f}).")
        else:
            kontext_texte.append(
                f"Die aktuelle Belastung entspricht in etwa dem langjährigen Durchschnitt der Region für diese Jahreszeit (Schnitt: {avg_heute:.1f}).")

    return " ".join(kontext_texte) if kontext_texte else "Zu wenig historische Daten für einen Trend."


# ========== 5. KI Generierung ==========
def generate_sda_pollen_article(region: str, triggering_alerts: list, other_alerts: list, current_context: str,
                                date_str: str, messdatum_str: str) -> str:
    if not GEMINI_API_KEY: return "FEHLER: Kein Gemini API Key gesetzt."

    alert_text = "=== AUSLÖSER DER MELDUNG (Massiver Anstieg / Neu) ===\n" + "\n".join(triggering_alerts)
    if other_alerts:
        alert_text += "\n\n=== EBENFALLS AKTUELL STARK (Aber ohne signifikanten Anstieg) ===\n" + "\n".join(
            other_alerts)

    system_instruction = """
    Du bist ein professioneller Nachrichtenredaktor für die Schweizer Nachrichtenagentur Keystone-SDA.
    Deine Aufgabe ist es, aus rohen Pollendaten und dem aktuellen Hintergrundbericht eine pfannenfertige Agenturmeldung zu verfassen (ca. 150 bis 250 Wörter).

    STRUKTUR UND REGELN (Zwingend exakt in dieser Reihenfolge einhalten!):

    1. TITEL: Format: "Gesundheit - Starke Pollenbelastung in [Region] (TT.MM.JJJJ)" 
       (WICHTIG: Verwende als Datum ZWINGEND das [Publikationsdatum]. Schreibe NIEMALS den Wochentag dazu!).

    2. ABSATZ 1 (Die Hauptnachricht - Auslöser & Mitschwimmer): 
       Baue den Lead EXAKT nach folgendem Muster auf (Passe die Platzhalter an die Daten unter "AUSLÖSER" an):
       "In [Region] hat die Belastung durch [Pollenarten] die [zweithöchste/höchste] Stufe '[stark/sehr stark]' erreicht. Mit einem Tagesdurchschnitt von [X] Pollen pro Kubikmeter ist sie am [Messdatum] im Vergleich zum Vortag ([Y]) [massiv gestiegen/gesunken/gleichgeblieben], wie die Daten von MeteoSchweiz am [Publikationsdatum] zeigten."

       Falls unter "EBENFALLS AKTUELL STARK" weitere Pollen gelistet sind, hänge diesen Satz zwingend direkt an: 
       "Zudem verharren auch die [Pollenarten] weiterhin auf einem starken Niveau."

    3. ABSATZ 2 (Skala & Historie):
       Übernimm ZWINGEND den Text zur Skalendefinition ("Die für [Art] erreichte Stufe...") exakt und wortwörtlich aus den gelieferten Daten. Falls mehrere Pollenarten betroffen sind, verbinde die Erklärungen sprachlich elegant.
       Erkläre direkt danach den historischen Saisonstart (früh/spät) und vergleiche die aktuelle Intensität mit dem langjährigen Durchschnitt der betroffenen Pflanzen.
       WICHTIG: Erwähne im Text ausdrücklich, dass sich diese historischen Daten auf die *jeweilige Region* beziehen.

    4. ABSATZ 3 (Hintergrund & Wetter): 
       Nutze den TAGESAKTUELLEN BERICHT von "aha! Allergiezentrum Schweiz". Formuliere das aktuelle Geschehen als harte Fakten im INDIKATIV. Verwende den Konjunktiv ("seien", "könnten") nur ganz am Schluss für theoretische Erklärungen.

    5. FORM: Schweizer Hochdeutsch (ss statt ß). Nur fliessender Pressetext, keine Aufzählungen! Nutze immer den Begriff "Kubikmeter" für das Volumen.
    """

    prompt = f"Publikationsdatum: {date_str}\nMessdatum: {messdatum_str}\nRegion: {region}\n\nPOLLENDATEN:\n{alert_text}\n\nHINTERGRUND:\n{current_context}"

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        config = types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
        return response.text.strip()
    except Exception as e:
        return f"KI-Generierung fehlgeschlagen: {e}"


# ========== 6. E-Mail Versand ==========
def send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        print("E-Mail-Konfiguration unvollständig – übersprungen.")
        return

    msg = MIMEMultipart()
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", _charset="utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"E-Mail erfolgreich gesendet: {subject}")
    except Exception as e:
        print(f"Fehler beim Mailversand: {e}")


# ========== 7. Hauptlauf ==========
def process_pollen():
    seen = _load_json(SEEN_FILE, {})
    today_str = datetime.now().strftime("%d.%m.%Y")
    seen_date_str = datetime.now().strftime("%Y-%m-%d")

    current_context = fetch_current_background_text()

    for station_id, region_name in STATIONS.items():
        if seen.get(f"{station_id}_run_date") == seen_date_str:
            continue

        print(f"Prüfe Station {station_id.upper()} ({region_name})...")
        csv_data_today, csv_data_last_week = fetch_latest_csv_data(station_id)
        if not csv_data_today: continue

        # ECHTES MESSDATUM AUS DER CSV AUSLESEN
        messdatum_str = ""
        for key, val in csv_data_today.items():
            if key and any(k in key.lower() for k in ["date", "datum", "time"]):
                raw_date = val[:10]
                try:
                    messdatum_str = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                except ValueError:
                    messdatum_str = raw_date
                break

        if not messdatum_str:
            messdatum_str = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")

        triggering_alerts = []
        other_strong_alerts = []
        send_mail_for_region = False

        for param, rules in THRESHOLDS.items():
            val_str = csv_data_today.get(param, "")
            if val_str:
                try:
                    val_float = float(val_str)
                    if val_float >= rules["stark"]:

                        if val_float >= rules["sehr_stark"]:
                            aktuelle_stufe = "sehr stark"
                            rang = "höchste"
                        else:
                            aktuelle_stufe = "stark"
                            rang = "zweithöchste"

                        skala_text = (
                            f'Die für {rules["name"]} erreichte Stufe "{aktuelle_stufe}" ist die {rang} auf der vierteiligen Pollen-Skala. '
                            f'Diese reicht von "schwach" ({rules["schwach"]} P/m³ - kaum Symptome) '
                            f'über "mässig" ({rules["maessig"]} P/m³, sensibilisierte Personen spüren es) '
                            f'bis zu "stark" ({rules["stark"]}-{rules["sehr_stark"] - 1} P/m³ - die meisten Allergiker haben deutliche Symptome) '
                            f'und "sehr stark" (ab {rules["sehr_stark"]} P/m³ - extreme Belastung, hohes Risiko für Asthma-Anfälle).'
                        )

                        prev_str = csv_data_last_week.get(param, "")
                        prev_float = float(prev_str) if prev_str else 0.0
                        if val_float > prev_float * 1.1:
                            trend = "gestiegen"
                        elif val_float < prev_float * 0.9:
                            trend = "gesunken"
                        else:
                            trend = "gleichbleibend hoch"

                        historie = get_historical_context(station_id, param, rules["stark"], val_float)

                        info_text = (
                            f"{rules['name']} (Aktueller Tagesdurchschnitt: {val_float} Pollenkörner/m³, Vortag: {prev_float} Pollenkörner/m³, Entwicklung zum Vortag: {trend}. "
                            f"Historischer Kontext: {historie} Skalendefinition: {skala_text})"
                        )

                        # Trigger Logik prüfen (Auslöser vs. Mitschwimmer)
                        last_val = seen.get(f"{station_id}_{param}", {}).get("value", 0.0)
                        if last_val == 0.0 or val_float >= (last_val * 1.5):
                            send_mail_for_region = True
                            triggering_alerts.append(info_text)
                        else:
                            other_strong_alerts.append(info_text)
                except ValueError:
                    pass

        # Nur wenn es einen Auslöser gibt, geht die Meldung raus
        if triggering_alerts and send_mail_for_region:
            print(f"ALARM in {region_name}! Generiere Meldung...")
            sda_article = generate_sda_pollen_article(region_name, triggering_alerts, other_strong_alerts,
                                                      current_context, today_str, messdatum_str)

            lines = sda_article.split("\n")
            mail_subject = lines[0] if lines else f"Pollen-Warnung: {region_name}"

            desk_notes = (
                "\n\n---\n"
                "HINWEIS FÜR DIE REDAKTION - QUELLEN ZUR VERIFIKATION:\n"
                "- Aktuelle Rohdaten des Bundes (CSV): https://opendata.swiss/de/dataset/pollenmessdaten\n"
                "- Hintergrundtext (aha! Allergiezentrum): https://www.pollenundallergie.ch/polleninformationen/pollenprognosen\n"
                "- Offizielle Grenzwerte & Skala (PDF): https://www.meteoschweiz.admin.ch/dam/jcr:5a46f08a-68a1-413c-9718-821e27540b27/Belastungsklassen-allergener-Pollenarten_D.pdf\n"
            )

            all_alerts_text = "\n".join(triggering_alerts) + (
                "\n" + "\n".join(other_strong_alerts) if other_strong_alerts else "")
            mail_body = f"{sda_article}\n\n---\nAutomatischer Trigger & Mitschwimmer:\n{all_alerts_text}{desk_notes}"

            print(f"Generierter Artikel:\n{mail_body}\n")

            # WICHTIG: Raute entfernen für echten E-Mail Versand!
            # send_email(mail_subject, mail_body)

            seen[f"{station_id}_run_date"] = seen_date_str
            for param in THRESHOLDS.keys():
                v_str = csv_data_today.get(param, "")
                if v_str:
                    try:
                        v_float = float(v_str)
                        if v_float >= THRESHOLDS[param]["stark"]:
                            seen[f"{station_id}_{param}"] = {"date": today_str, "value": v_float}
                    except ValueError:
                        pass
            _save_json(SEEN_FILE, seen)

        elif triggering_alerts or other_strong_alerts:
            print(f"Info für {region_name}: Werte stark, aber nicht signifikant gestiegen. Keine Mail.")
            seen[f"{station_id}_run_date"] = seen_date_str
            _save_json(SEEN_FILE, seen)
        else:
            print(f"Keine relevanten Werte für {region_name} (unter Stufe Stark).")


if __name__ == "__main__":
    print("Starte Pollen SDA-Agent...")
    process_pollen()
    print("Lauf beendet.")