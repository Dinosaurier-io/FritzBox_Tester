---
name: neue-metrik
description: Checkliste zum Hinzufügen eines Messwerts, Ereignistyps oder ganzen Moduls - von core/models.py über Datenbank und Excel-Export bis zu Bericht, Diagramm und Test. Verwenden, wenn etwas Neues gemessen, ein neuer Ereignistyp eingeführt oder ein Modul ergänzt werden soll.
---

# Neue Metrik, neues Ereignis, neues Modul

Ein neuer Messwert berührt sieben Stellen. Wird eine übersprungen, gibt es **keine
Fehlermeldung** — der Wert fehlt still in Export, Bericht oder Diagramm. Deshalb reihum
durchgehen und am Ende abhaken, was zutrifft und was bewusst nicht.

## 1. Modell (`core/models.py`)

- **Messwert:** meist nichts zu tun. `Measurement` ist absichtlich generisch
  (`module`/`metric`/`value`/`unit`/`meta`), damit eine neue Metrik keine Schemaänderung
  erzwingt. Nur wenn ein ganz neuer Datentyp entsteht (nicht Messwert, Ereignis, Router-,
  WLAN-Status, Speedtest, Ausfall), braucht es eine neue Dataclass **und** einen neuen Zweig
  im `Payload`-Union in `core/events.py` sowie im `DatabaseWriter`.
- **Ereignistyp:** neues Mitglied in `EventType`, mit Docstring darunter, der erklärt *wozu*
  der Typ da ist — nicht was er heisst. Die Namen sind bewusst stabil: Sie stehen in
  Datenbank, Export und Bericht und müssen zwischen Testläufen vergleichbar bleiben. Also
  nie umbenennen, nur ergänzen.

## 2. Erzeugende Stelle (`modules/`)

Innerhalb eines Moduls `self.measure("metrik", wert, "einheit", **meta)` bzw.
`self.emit(EventType.X, "deutsche Klartextmeldung", severity)`. Der Modulname kommt aus
`name` der Klasse und landet als `module` in der Datenbank — daraus wird später der
Selektorschlüssel `modul:metrik`.

Ein neues Modul erbt von `MonitorModule` oder `IntervalModule` (`modules/base.py`), kennt
**nur** den Bus und wird in `runner.py::_build_modules` registriert. Es darf abstürzen — der
Scheduler startet es mit Backoff neu.

Netzwerkaufrufe über Subprozesse: `**hidden_process_kwargs()` aus `proc.py` mitgeben,
sonst blitzen unter Windows Konsolenfenster auf. `test_proc.py` prüft das.

## 3. Datenbank (`storage/database.py`)

Messwerte und Ereignisse brauchen kein Schema. Eine **neue Tabelle** dagegen:
`_SCHEMA` ergänzen, `SCHEMA_VERSION` erhöhen, Eintrag in `_TABLE_ORDER` (Tabellenname →
Sortierspalte). `_TABLE_ORDER` ist zugleich die Positivliste gegen SQL-Injection in
`iter_table`/`fetch_table`/`count_rows` — was dort fehlt, ist nicht lesbar.

## 4. Excel-Export (`storage/workbook.py`)

Neue Tabelle → neues Blatt in `export_workbook`. Neue Metrik → erscheint automatisch im
Blatt „Messreihen" (über `list_metric_series`) und in „Messwerte". Soll sie in der
**Übersicht** stehen, in `_build_overview` ergänzen.

Beim Anlegen eines Blattes `sheet.freeze_panes` als Zeichenkette setzen
(`f"A{sheet.max_row + 1}"`) — der Zugriff über `sheet.cell(...)` legt die Zelle an und
erzeugt eine leere Zeile unter der Kopfzeile.

## 5. Bericht (`report/generator.py`)

Kennzahl für die Übersicht: `Metric` in `analyze_run` ergänzen, mit
`direction` (`lower` = kleiner ist besser, `higher`, `neutral`). Die Richtung ist keine
Kosmetik — der Vergleichsmodus bewertet daran „B ist besser/schlechter". Werte, die vor
allem an der Laufzeit hängen (Volumen, Zählwerte), sind `neutral`.

Die Auswertung bleibt **I/O-frei**. Kein Netzwerkzugriff, sonst ist die Testsuite nicht mehr
netzunabhängig.

## 6. Diagramm im Bericht (`report/generator.py`, `report/charts.py`)

Soll die Metrik als Kurve erscheinen, in `analyze_run` unter `analysis.charts` ergänzen.
Das Dashboard zeigt bewusst **keine** Verläufe mehr — während eines Laufs sind sie kaum
lesbar; alles Kurvenhafte steht im Bericht.

**Der Metrikname muss exakt dem entsprechen, was das Modul veröffentlicht.** Ein Tippfehler
erzeugt keine Fehlermeldung, sondern ein dauerhaft leeres Diagramm — genau so ist
`packet_loss_pct` monatelang unbemerkt geblieben, obwohl die Metrik `loss_pct` heisst.
`test_core.py::test_report_metrics_are_actually_recorded` prüft das auf Quelltextebene.

Mehrere Kurven in einem Bild brauchen sprechende Namen: `_series_by_meta(..., detail="host")`
hängt die Adresse an den Zielnamen, sonst steht in der Legende nur „1" und niemand weiss,
welche Kurve wohin gemessen hat.

Gegenprobe statt Augenschein:

```powershell
.\.venv\Scripts\python.exe -c @'
from pathlib import Path
from fbtest.storage.database import Database
db_path = Path.home() / "AppData/Local/fbtest/data/fbtest.sqlite"
with Database(db_path) as db:
    for row in db.list_metric_series(7):
        print(f"{row['module']}:{row['metric']}", row["unit"])
'@
```

## 7. Test und README

Test dorthin, wo ein Fehler teuer wäre: Parser und Auswertelogik ja, Verdrahtung meist nein.
Keine `@pytest.mark.asyncio`-Marker (`asyncio_mode = "auto"`).

In `README.md` die Testanzahl an beiden Stellen nachziehen; eine neue Testdatei zusätzlich in
die Tabelle im Abschnitt „Entwicklung und Qualitätssicherung" eintragen, mit einer Zeile dazu,
*was* sie prüft.

## Konfiguration

Neue Konfigurationsfelder brauchen **keine** Formularpflege: Die Einstellungsmaske entsteht aus
dem JSON-Schema der pydantic-Modelle. Ein Feld in `config.py` mit `description` erscheint
automatisch mit Beschriftung und Wertebereich. Zusätzlich in `resources/config.example.yaml`
kommentiert eintragen — `test_config.py` prüft, dass die ausgelieferte Vorlage gültig bleibt.
