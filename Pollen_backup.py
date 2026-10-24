# -*- coding: utf-8 -*-
"""
PollenSDA -> Web-Scraping & CSV -> Gemini KI -> SDA-Meldung -> E-Mail (BILINGUAL)
- Zieht OGD-Pollen-CSV des Bundes
- Übersetzungsmatrix für DE/FR
- Zwei getrennte KI-Prompts (SDA / ATS)
- Getrennter Mailversand an DE- und FR-Redaktion
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
from pymongo import MongoClient
import certifi

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
EMAIL_RECEIVER_DE = CONFIG.get("EMAIL_RECEIVER_DE", "")
EMAIL_RECEIVER_FR = CONFIG.get("EMAIL_RECEIVER_FR", "")

SEEN_FILE = "seen_pollen.json"

# ========== 2. Zweisprachige Matrix (Dictionaries) ==========
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
            "kaquerd0": {"de": "Eiche", "fr": "Chêne", "url_name": "eiche"},  # Jetzt korrekt!
            "kafagud0": {"de": "Buche", "fr": "Hêtre", "url_name": "buche"}    # Jetzt korrekt!
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
    "kaquerd0": {"schwach": "1-49", "maessig": "50-129", "stark": 130, "sehr_stark": 400},  # Jetzt korrekt!
    "kafagud0": {"schwach": "1-49", "maessig": "50-129", "stark": 130, "sehr_stark": 400}   # Jetzt korrekt!
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


NORM_FILE = "pollen_normwerte_1991_2020.json"
NORMWERTE = _load_json(NORM_FILE, {})


# ========== 4. Daten-Beschaffung ==========
def fetch_current_background_text(lang: str) -> str:
    url = DICT["aha_urls"][lang]
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
                if not rows: continue

                # Wir gehen von der neuesten Zeile rückwärts, bis wir eine mit Daten finden
                valid_row = None
                valid_prev_row = None

                for i in range(len(rows) - 1, -1, -1):
                    # Testen, ob in den wichtigsten Pollenfeldern überhaupt Werte stehen
                    if rows[i].get('khpoacd0') and rows[i]['khpoacd0'].strip() != '':
                        valid_row = rows[i]
                        # Die Vorperiode ist dann einfach die Zeile davor
                        valid_prev_row = rows[i - 1] if i > 0 else rows[i]
                        break

                # Falls im ganzen File nur Leeres steht, nimm als Fallback die letzte Zeile
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
        if lang == "de":
            return "Für diese Region/Pollenart liegen keine ausreichenden Klimanormwerte (1991-2020) vor."
        else:
            return "Données climatiques (1991-2020) insuffisantes pour cette région/ce pollen."

    current_year = datetime.now().year
    today_yday = datetime.now().timetuple().tm_yday
    avg_start_yday = param_data.get("avg_start_yday")
    avg_heute = param_data.get("daily_normals", {}).get(str(today_yday), 0.0)

    kontext_texte = []

    if avg_start_yday:
        avg_date = datetime(current_year, 1, 1) + timedelta(days=avg_start_yday - 1)
        diff = today_yday - avg_start_yday
        if diff < -7:
            kontext_texte.append(
                f"Der Saisonstart erfolgte extrem früh (Schnitt 1991–2020: um den {avg_date.strftime('%d.%m.')})." if lang == "de" else f"Le début de saison a été très précoce (Moyenne 1991-2020: vers le {avg_date.strftime('%d.%m.')}).")


    if avg_heute > 0:
        if current_val > (avg_heute * 1.5):
            kontext_texte.append(
                f"Aktuelle Belastung liegt massiv über dem langjährigen Schnitt (Klimanorm: {avg_heute:.1f} P/m³)." if lang == "de" else f"La charge actuelle est massivement supérieure à la moyenne à long terme (Norme climatique: {avg_heute:.1f} P/m³).")
        elif current_val < (avg_heute * 0.5):
            kontext_texte.append(
                f"Aktuelle Belastung tiefer als langjähriger Schnitt (Klimanorm: {avg_heute:.1f} P/m³)." if lang == "de" else f"La charge actuelle est inférieure à la moyenne à long terme (Norme climatique: {avg_heute:.1f} P/m³).")

    return " ".join(kontext_texte) if kontext_texte else (
        "Kein Vergleich möglich." if lang == "de" else "Aucune comparaison possible.")


# ========== 5. KI Generierung ==========
def generate_sda_pollen_article(lang: str, region: str, triggering_alerts: list, other_alerts: list,
                                current_context: str, date_str: str, messdatum_str: str, wochentag: str) -> str:
    if not GEMINI_API_KEY: return "FEHLER: API Key fehlt."

    if lang == "de":
        alert_text = "=== AUSLÖSER DER MELDUNG (Massiver Anstieg / Neu) ===\n" + "\n".join(triggering_alerts)
        if other_alerts: alert_text += "\n\n=== EBENFALLS AKTUELL STARK ===\n" + "\n".join(other_alerts)

        system_instruction = """
        Du bist Redaktor für Keystone-SDA. Verfasse eine Agenturmeldung (150-250 Wörter).
        1. TITEL: "Gesundheit - Starke Pollenbelastung in [Region] (TT.MM.JJJJ)" (Datum=Publikationsdatum).
        2. LEAD: "In [Region] hat die Belastung durch [Pollen] die [zweithöchste/höchste] Stufe '[stark/sehr stark]' erreicht. Mit einem Tagesdurchschnitt von [X] Pollen pro Kubikmeter ist sie am [Messdatum] im Vergleich zum Vortag ([Y]) massiv gestiegen, wie MeteoSchweiz am [Wochentag] zeigte." (Bei weiteren Pollen anfügen: "Zudem verharren auch [Pollen] auf starkem Niveau.")
        3. SKALA & HISTORIE: Übernimm die mitgelieferte Skalendefinition exakt. Erkläre den historischen Saisonstart und Intensität für die Region.
        4. HINTERGRUND: Nutze den Bericht von aha!. Nur Indikativ.
        """
    else:
        alert_text = "=== DÉCLENCHEURS DE L'ALERTE (Forte hausse / Nouveau) ===\n" + "\n".join(triggering_alerts)
        if other_alerts: alert_text += "\n\n=== ÉGALEMENT FORT ACTUELLEMENT ===\n" + "\n".join(other_alerts)

        system_instruction = """
            Tu es rédacteur pour l'agence de presse Keystone-ATS. Rédige le texte d'une dépêche (150-250 mots).

            RÈGLES DE STYLE STRICTES :
            - N'écris AUCUN titre. N'écris PAS d'amorce géographique ou d'agence (ne commence PAS par "Zurich (ATS) -").
            - Fais des phrases courtes et percutantes. Divise les phrases longues (ex: utilise un point ". Elles entraînent..." au lieu d'une virgule ", entraînant...").
            - Utilise ". Les spécialistes parlent dans ce cas d'allergies croisées." au lieu de ", on parle alors...".
            - Utilise des guillemets classiques ("fort") et le terme "moyenne quotidienne".

            STRUCTURE IMPOSÉE :
            1. LEAD : Commence directement par cette phrase : "La concentration de pollens de [Pollens] a atteint le niveau "[fort/très fort]" dans la région [Région], selon les données de MétéoSuisse publiées [Jour de la semaine]. Avec une moyenne quotidienne de [X] pollens par mètre cube, elle a massivement augmenté par rapport à la veille ([Y])." (S'il y a d'autres pollens, ajoute : "Les pollens de [Autres pollens] se maintiennent à un niveau élevé, tandis que ceux de [Autres] sont en hausse.")
            2. ÉCHELLE & HISTORIQUE : Explique l'échelle : "Le niveau "[fort]" signifie que..." Sépare l'historique en phrases courtes : "Le début de saison pour le [Pollen] a été plutôt tardif/précoce cette année. La moyenne sur la période 1991-2020 se situe autour du [Date]."
            3. CONTEXTE : Résume les informations de "aha! Centre d'Allergie" de manière très factuelle, toujours avec des phrases courtes.
            """

    prompt = f"Publikationsdatum/Date: {date_str}\nMessdatum: {messdatum_str}\nWochentag/Jour: {wochentag}\nRegion: {region}\n\nDATEN/DONNÉES:\n{alert_text}\n\nHINTERGRUND/CONTEXTE:\n{current_context}"

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        config = types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
        return response.text.strip()
    except Exception as e:
        return f"KI-Error: {e}"


# ========== 6. E-Mail Versand ==========
def send_email(subject: str, body: str, receiver: str):
    print(f" -> Versuche Mail zu senden an {receiver} via {EMAIL_SENDER}...")

    if not all([EMAIL_SENDER, EMAIL_PASSWORD, receiver]):
        print(" -> ❌ Mail abgebrochen: Zugangsdaten in config.json fehlen!")
        return

    msg = MIMEMultipart()
    msg["From"] = EMAIL_SENDER
    msg["To"] = receiver
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", _charset="utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
            print(f" -> ✅ Mail erfolgreich gesendet an {receiver}")
    except Exception as e:
        print(f" -> ❌ Fehler Mailversand: {e}")


# ========== 7. Hauptlauf ==========
def process_pollen():
    seen = _load_json(SEEN_FILE, {})
    today_str = datetime.now().strftime("%d.%m.%Y")
    seen_date_str = datetime.now().strftime("%Y-%m-%d")
    weekday_idx = datetime.now().weekday()

    # Lade Hintergründe vorab
    bg_texts = {"de": fetch_current_background_text("de"), "fr": fetch_current_background_text("fr")}

    for station_id in DICT["stations"].keys():
        if seen.get(f"{station_id}_run_date") == seen_date_str: continue

        print(f"Prüfe Station {station_id.upper()}...")
        csv_today, csv_last = fetch_latest_csv_data(station_id)
        if not csv_today:
            print(
                f" -> ❌ Download fehlgeschlagen! Der Bund liefert unter diesem Link keine Daten für {station_id.upper()}.")
            continue

        messdatum_str = today_str
        print("Gefundene Spalten:", list(csv_today.keys()))
        for key, val in csv_today.items():
            if key and any(k in key.lower() for k in ["date", "datum", "time"]):
                try:
                    messdatum_str = datetime.strptime(val[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
                except ValueError:
                    pass
                break

        # Daten für BEIDE Sprachen sammeln
        alerts = {"de": {"trigger": [], "other": [], "links": [], "desk": []},
                  "fr": {"trigger": [], "other": [], "links": [], "desk": []}}

        has_trigger = False

        for param, rules in THRESHOLDS.items():
            val_str = csv_today.get(param, "")

            print(f"   -> Wert für {param}: '{val_str}'")
            if not val_str: continue
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

                    # Logik: Ist es ein Trigger?
                    last_val = seen.get(f"{station_id}_{param}", {}).get("value", 0.0)
                    is_trigger = True if (trend_key == "gestiegen" and (
                                last_val == 0.0 or val_float >= last_val * 1.5)) else False
                    if is_trigger: has_trigger = True

                    # Text für DE und FR aufbauen
                    for lang in ["de", "fr"]:
                        name = p_info[lang]
                        trend_txt = DICT["trends"][trend_key][lang]
                        hist_txt = get_historical_context(station_id, param, rules["stark"], val_float, lang)

                        if val_float >= rules["stark"]:
                            skala = "Die höchste Stufe 'sehr stark' bedeutet für Allergiker extremes Risiko für Asthma-Anfälle." if lang == "de" else "Le niveau 'très fort' signifie un risque extrême de crise d'asthme pour les allergiques."
                        else:
                            skala = "Die Stufe 'stark' bedeutet, dass die meisten Allergiker deutliche Symptome zeigen." if lang == "de" else "Le niveau 'fort' signifie que la plupart des personnes allergiques présentent des symptômes clairs."

                        info_txt = f"{name} ({val_float} P/m³, Vortag/Veille: {prev_float}, Trend: {trend_txt}. Historie: {hist_txt} Skala: {skala})"

                        if is_trigger:
                            alerts[lang]["trigger"].append(info_txt)
                        else:
                            alerts[lang]["other"].append(info_txt)

                        # Desk Notes (Links)
                        st_name = DICT["stations"][station_id][lang]
                        if lang == "de":
                            url = f"https://www.meteoschweiz.admin.ch/service-und-publikationen/applikationen/messwerte.html#param=messwerte-pollen-{p_info['url_name']}-1h&station={station_id.upper()}&table=false"
                            link_txt = f"- Verifikation {name} in {st_name}: {url}"
                            desk_txt = f'- {name}: "schwach" ({rules["schwach"]}), "mässig" ({rules["maessig"]}), "stark" ({rules["stark"]}-{rules["sehr_stark"] - 1}), "sehr stark" (ab {rules["sehr_stark"]}).'
                        else:
                            url = f"https://www.meteosuisse.admin.ch/services-et-publications/applications/valeurs-mesurees.html#param=messwerte-pollen-{p_info['url_name']}-1h&station={station_id.upper()}&table=false"
                            link_txt = f"- Vérification {name} à {st_name}: {url}"
                            desk_txt = f'- {name}: "faible" ({rules["schwach"]}), "modéré" ({rules["maessig"]}), "fort" ({rules["stark"]}-{rules["sehr_stark"] - 1}), "très fort" (dès {rules["sehr_stark"]}).'

                        if link_txt not in alerts[lang]["links"]: alerts[lang]["links"].append(link_txt)
                        if desk_txt not in alerts[lang]["desk"]: alerts[lang]["desk"].append(desk_txt)

            except ValueError:
                pass

        # Mail-Versand für beide Sprachen, falls Trigger vorhanden
        if has_trigger:
            print(f"ALARM in {station_id.upper()}! Generiere Mails...")
            for lang, receiver in [("de", EMAIL_RECEIVER_DE), ("fr", EMAIL_RECEIVER_FR)]:
                if not alerts[lang]["trigger"]: continue  # Sicherheitshalber

                reg_name = DICT["stations"][station_id][lang]
                wochentag = DICT["wochentage"][lang][weekday_idx]

                article = generate_sda_pollen_article(
                    lang, reg_name, alerts[lang]["trigger"], alerts[lang]["other"],
                    bg_texts[lang], today_str, messdatum_str, wochentag
                )

                subj = article.split("\n")[0] if article else f"Pollen: {reg_name}"

                if lang == "de":
                    notes = "\n\n---\nQUELLEN ZUR VERIFIKATION:\n" + "\n".join(alerts["de"][
                                                                                   "links"]) + f"\n\nHintergrund: {DICT['aha_urls']['de']}\n\nSchwellenwerte:\n" + "\n".join(
                        alerts["de"]["desk"])
                    body = f"{article}\n\n---\nTrigger:\n" + "\n".join(alerts["de"]["trigger"]) + notes
                else:
                    notes = "\n\n---\nSOURCES POUR VÉRIFICATION:\n" + "\n".join(alerts["fr"][
                                                                                    "links"]) + f"\n\nContexte: {DICT['aha_urls']['fr']}\n\nSeuils:\n" + "\n".join(
                        alerts["fr"]["desk"])
                    body = f"{article}\n\n---\nDéclencheurs:\n" + "\n".join(alerts["fr"]["trigger"]) + notes

                # WICHTIG: Raute entfernen!
                #send_email(subj, body, receiver)
                print(f"--- [{lang.upper()}] --- \n{body}\n")

            # 1. Bei einem Alarm merken wir uns den neuen hohen Wert
            for p, r in THRESHOLDS.items():
                v = csv_today.get(p, "")
                if v and float(v) >= r["stark"]:
                    seen[f"{station_id}_{p}"] = {"date": today_str, "value": float(v)}

        elif any(alerts["de"]["other"]):
            print(f"Info für {station_id}: Stark, aber kein Anstieg.")

        # 2. NEU: Reset-Logik! Wenn ein Pollenwert wieder sinkt (Regen, Saisonende, Winter), löschen wir das Gedächtnis
        for p, r in THRESHOLDS.items():
            v = csv_today.get(p, "")
            if v:
                try:
                    if float(v) < r["stark"]:
                        if f"{station_id}_{p}" in seen:
                            seen[f"{station_id}_{p}"] = {"date": today_str, "value": 0.0}
                except ValueError:
                    pass

        # 3. IMMER das Datum des heutigen Checks speichern (egal ob Alarm, stark oder schwach)
        seen[f"{station_id}_run_date"] = seen_date_str
        _save_json(SEEN_FILE, seen)


if __name__ == "__main__":
    process_pollen()