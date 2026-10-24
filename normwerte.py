# -*- coding: utf-8 -*-
"""
Einmaliges Setup-Skript für Pollen-Klimanormwerte (1991-2020)
Lädt historische Daten, berechnet geglättete Tagesdurchschnitte und Saisonstarts.
"""
import requests
import csv
from io import StringIO
from datetime import datetime
import json

STATIONS = ["pge", "pls", "pne", "pbs", "psn", "pbe", "plu", "pmu", "pzh", "pds", "plo", "pbu"]
THRESHOLDS = {
    "kabetud0": 70, "khpoacd0": 50, "kafraxd0": 100, "kacoryd0": 70,
    "kaalnud0": 70, "kquercd0": 130, "kfagusd0": 130, "kplatad0": 100, "kcarpid0": 70
}


def fetch_historical_data(station_id):
    headers = {"User-Agent": "Mozilla/5.0"}
    url_sub = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/{station_id}/ogd-pollen_{station_id}_d_historical.csv"
    url_root = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-pollen/ogd-pollen_{station_id}_d_historical.csv"
    for url in [url_sub, url_root]:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code == 200:
                r.encoding = 'utf-8'
                return r.text
        except:
            continue
    return ""


def calculate_normals():
    print("Starte Berechnung der Klimanormwerte 1991-2020...")
    norm_data = {}

    for station in STATIONS:
        print(f"Lade Archiv für {station.upper()}...")
        csv_text = fetch_historical_data(station)
        if not csv_text:
            continue

        norm_data[station] = {}
        lines = csv_text.splitlines()
        if not lines: continue

        # Header suchen
        header_idx, delimiter = -1, ';'
        for i, line in enumerate(lines[:20]):
            if 'kabetud0' in line.lower():
                header_idx = i
                delimiter = ';' if ';' in line else ','
                break

        if header_idx == -1: continue
        headers = [h.strip().replace('"', '').lower() for h in lines[header_idx].replace('\ufeff', '').split(delimiter)]

        date_idx = next((i for i, h in enumerate(headers) if any(k in h for k in ["date", "datum", "time"])), 0)

        # Datenstrukturen für 1991-2020
        raw_values = {param: {yday: [] for yday in range(1, 367)} for param in THRESHOLDS.keys()}
        start_days = {param: {} for param in THRESHOLDS.keys()}  # year -> yday

        for line in lines[header_idx + 1:]:
            row = line.split(delimiter)
            if len(row) <= date_idx: continue

            date_str = row[date_idx].strip().replace('"', '')[:10]
            try:
                if '-' in date_str:
                    d_obj = datetime.strptime(date_str, "%Y-%m-%d")
                else:
                    d_obj = datetime.strptime(date_str, "%d.%m.%Y")
            except ValueError:
                continue

            year = d_obj.year
            yday = d_obj.timetuple().tm_yday

            # STRIKTER FILTER: Nur Referenzperiode 1991-2020
            if year < 1991 or year > 2020: continue

            for param, threshold in THRESHOLDS.items():
                if param not in headers: continue
                param_idx = headers.index(param)
                if len(row) > param_idx:
                    val_str = row[param_idx].strip().replace('"', '').replace(',', '.')
                    try:
                        val_float = float(val_str)
                        raw_values[param][yday].append(val_float)

                        # Saisonstart (erster Tag über Schwelle im Jahr)
                        if val_float >= threshold:
                            if year not in start_days[param]:
                                start_days[param][year] = yday
                    except ValueError:
                        pass

        # Durchschnitt und 7-Tage-Glättung berechnen
        for param in THRESHOLDS.keys():
            daily_avg = {}
            for yday in range(1, 367):
                vals = raw_values[param][yday]
                daily_avg[yday] = sum(vals) / len(vals) if vals else 0.0

            smoothed_avg = {}
            # 7-Tage-Glättung (Window = 7)
            for yday in range(1, 367):
                window_vals = []
                for j in range(yday - 3, yday + 4):
                    idx = j
                    if idx < 1: idx += 365
                    if idx > 366: idx -= 365
                    window_vals.append(daily_avg[idx])
                smoothed_avg[yday] = round(sum(window_vals) / len(window_vals), 1)

            avg_start = None
            if len(start_days[param]) >= 10:  # Mindestens 10 Jahre Daten benötigt für seriösen Schnitt
                avg_start = int(sum(start_days[param].values()) / len(start_days[param]))

            # Nur speichern, wenn wir überhaupt Messungen in dieser Periode hatten
            if sum(smoothed_avg.values()) > 0:
                norm_data[station][param] = {
                    "avg_start_yday": avg_start,
                    "daily_normals": smoothed_avg
                }

    with open("pollen_normwerte_1991_2020.json", "w", encoding="utf-8") as f:
        json.dump(norm_data, f, ensure_ascii=False, indent=2)
    print("Fertig! Datei 'pollen_normwerte_1991_2020.json' erstellt.")


if __name__ == "__main__":
    calculate_normals()