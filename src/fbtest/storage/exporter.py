"""Export eines Testlaufs als CSV oder JSON.

CSV wird je Tabelle in eine eigene Datei geschrieben (eine einzige Datei mit
gemischten Spalten waere nicht auswertbar), JSON dagegen als eine
zusammenhaengende Struktur - so laesst sich ein kompletter Lauf mit einer Datei
weitergeben.

Der Export ist bewusst roh: keine Aggregation, keine Filterung. Wer die Daten in
Excel, R oder Python weiterverarbeiten will, bekommt genau das, was gemessen
wurde.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from fbtest.storage.database import Database

log = logging.getLogger(__name__)

#: Alle Tabellen, die zu einem Testlauf gehoeren.
EXPORT_TABLES = ("measurements", "events", "router_status", "wlan_status", "speedtests", "outages")


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Wandelt Datenbankzeilen in einfache Dictionaries."""
    return [dict(row) for row in rows]


def export_csv(db: Database, run_id: int, target_dir: Path) -> list[Path]:
    """Exportiert einen Testlauf als CSV-Dateien.

    Args:
        db: Geoeffnete Datenbank.
        run_id: ID des Testlaufs.
        target_dir: Zielordner; wird bei Bedarf angelegt.

    Returns:
        Liste der geschriebenen Dateien.

    Raises:
        ValueError: Wenn der Testlauf nicht existiert.
    """
    run = db.get_test_run(run_id)
    if run is None:
        raise ValueError(f"Testlauf #{run_id} existiert nicht.")

    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for table in EXPORT_TABLES:
        rows = _rows_to_dicts(db.fetch_table(table, run_id))
        path = target_dir / f"run{run_id:04d}_{table}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            if not rows:
                handle.write("")
            else:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
        written.append(path)
        log.info("%-15s -> %s (%d Zeilen)", table, path.name, len(rows))

    # Zusaetzlich die Metadaten des Laufs selbst.
    meta_path = target_dir / f"run{run_id:04d}_testrun.csv"
    with meta_path.open("w", encoding="utf-8-sig", newline="") as handle:
        meta_writer = csv.writer(handle, delimiter=";")
        meta_writer.writerow(["feld", "wert"])
        meta_writer.writerow(["id", run.id])
        meta_writer.writerow(["name", run.name])
        meta_writer.writerow(["gestartet_utc", run.started_at])
        meta_writer.writerow(["beendet_utc", run.ended_at or ""])
        meta_writer.writerow(["dauer_s", round(run.duration_s, 1)])
        meta_writer.writerow(["firmware", run.firmware_version or ""])
        meta_writer.writerow(["modell", run.router_model or ""])
    written.append(meta_path)

    return written


def export_json(db: Database, run_id: int, target_dir: Path) -> Path:
    """Exportiert einen Testlauf als eine einzelne JSON-Datei.

    Args:
        db: Geoeffnete Datenbank.
        run_id: ID des Testlaufs.
        target_dir: Zielordner; wird bei Bedarf angelegt.

    Returns:
        Pfad der geschriebenen Datei.

    Raises:
        ValueError: Wenn der Testlauf nicht existiert.
    """
    run = db.get_test_run(run_id)
    if run is None:
        raise ValueError(f"Testlauf #{run_id} existiert nicht.")

    target_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "test_run": {
            "id": run.id,
            "name": run.name,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "duration_s": round(run.duration_s, 1),
            "firmware_version": run.firmware_version,
            "router_model": run.router_model,
            "notes": run.notes,
        }
    }
    for table in EXPORT_TABLES:
        payload[table] = _rows_to_dicts(db.fetch_table(table, run_id))

    path = target_dir / f"run{run_id:04d}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("JSON-Export geschrieben: %s", path)
    return path
