# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`fbtest` — automatisiertes Langzeit-Testsystem für FRITZ!Box-Router (Python 3.14). Läuft als
Kommandozeilenwerkzeug, als Web-Dashboard und als gebündelte Desktop-Anwendung
(pywebview + PyInstaller). Zweck: Firmware-Versionen objektiv vergleichbar messen.

Die `README.md` beschreibt Funktionsumfang, Bedienung und Grenzen ausführlich — dort
nachschlagen statt hier zu duplizieren. Diese Datei enthält nur, was beim Arbeiten am
Code nötig ist.

## Befehle

Immer den Interpreter der virtuellen Umgebung verwenden, nicht das globale `python`:

```powershell
.\.venv\Scripts\python.exe -m pytest                       # gesamte Testsuite
.\.venv\Scripts\python.exe -m pytest tests/test_storage.py  # eine Datei
.\.venv\Scripts\python.exe -m pytest tests/test_storage.py::TestExport::test_csv -q
.\.venv\Scripts\python.exe -m pytest -k workbook            # nach Namen filtern
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format .
.\.venv\Scripts\python.exe -m mypy
.\packaging\build.ps1                                       # Programmpaket (Linux: build.sh)
```

Die Testsuite läuft ohne Netzwerk und ohne FRITZ!Box — das ist Absicht und muss so bleiben.
`asyncio_mode = "auto"`, also **keine** `@pytest.mark.asyncio`-Marker setzen.

`addopts = "-q"` steht schon in `pyproject.toml`. Ein zusätzliches `-q` auf der Kommandozeile
ergibt `-qq` und unterdrückt die Zeile „404 passed" — für die Testanzahl also ohne `-q`
aufrufen.

## Architektur

Der Datenfluss ist in der README als Diagramm dargestellt. Entscheidend beim Ändern von Code:

**Ein Bus, genau ein Schreiber.** Module (`modules/`) kennen weder einander noch die
Datenbank; sie legen `Measurement`/`Event`/`RouterStatus`/`WlanStatus`/`SpeedtestResult`/`Outage`
auf den `EventBus` (`core/events.py`). Einziger Konsument ist der `DatabaseWriter`
(`storage/writer.py`), der gebündelt schreibt. Ein zweiter Konsument würde Messwerte
abgreifen, die dann nie in der Datenbank landen — für Live-Anzeigen stattdessen
`bus.subscribe()` (Broadcast) verwenden.

**Auswertelogik ist I/O-frei.** `OutageTracker`, `UptimeTracker`, die Ping- und WLAN-Parser
und die Aggregation in `report/generator.py` machen keine Netzwerkzugriffe. Nur deshalb ist
die Testsuite netzunabhängig. Neue Analyselogik gehört in diese Schicht, nicht in die Module.

**Dauern über `time.monotonic()`, Zeitstempel über `time.time()`.** Zeitumstellung und
Standby dürfen keine Ausfalldauer verfälschen. `core/models.py` führt beides getrennt.

**Der `Scheduler`** (`core/scheduler.py`) überwacht alle Module und startet abgestürzte mit
Backoff (1 s → 60 s) neu. Ein Modul darf also abstürzen — es darf nur nicht den Lauf mitreißen.

**Der `TrafficGate`** lässt den Speedtest den Traffic-Generator pausieren, damit die eigene
Hintergrundlast die Bandbreitenmessung nicht verfälscht.

**Das Dashboard hat zwei Datenquellen mit zwei Takten:** Live-Zustand (2 s) direkt aus den
Modulinstanzen im Speicher, Ereignisse (15 s) aus der Datenbank. Wer das vermischt, bekommt
eine „Live"-Anzeige, die nur einmal pro Aggregationsfenster zuckt.

**Kurvenverläufe gehören in den Bericht, nicht ins Dashboard.** Während eines Laufs hat eine
Kurve zu wenige Punkte, um etwas auszusagen, verdrängt aber den aktuellen Zustand. Der
Live-Reiter enthält deshalb kein `<canvas>`; `test_dashboard.py` hält das fest.

**Arbeitsverzeichnis** wird von `paths.py` nach fünf Regeln ermittelt (`--config` →
`FBTEST_HOME` → `config.yaml` im CWD → Projektordner → `%LOCALAPPDATA%\fbtest\`). Nie
Pfade relativ zum Programmordner annehmen: Im gebündelten Zustand ist der schreibgeschützt.

## Feste Regeln

Diese stammen aus der Aufgabenstellung und stehen nicht zur Disposition:

- **Kommentare, Docstrings (Google-Style) und alle Benutzerausgaben auf Deutsch,
  Bezeichner auf Englisch.** Type Hints durchgehend.
- **Das Programm muss vollständig offline funktionieren.** Keine CDN-Links, kein npm, keine
  externen Schriften oder Skripte in `dashboard/static/`. Ein Werkzeug, das Internetausfälle
  misst, darf zur Anzeige seiner Messwerte kein Internet brauchen. `test_dashboard.py` prüft
  das — dieser Test darf nie „angepasst" werden, um eine Änderung durchzubekommen.
- **Passwort nie im Klartext** in `config.yaml`, Log oder Datenbank-Snapshot. Reihenfolge:
  `FRITZ_PASSWORD` → Schlüsselspeicher → (Notnagel) Klartext. Die Umgebungsvariable behält
  Vorrang.
- **Kein Netzwerkzugang zum Dashboard.** Bleibt auf `127.0.0.1`, keine Benutzeranmeldung,
  Schutz gegen fremde Seiten über das Sitzungs-Token (kein Cookie — das würde der Browser
  auch fremden Seiten mitsenden).
- **Die CLI bleibt vollwertig.** Kein Funktionsumfang, den es nur in der Oberfläche gibt.
- **Kein Subprocess der eigenen CLI aus der GUI**, keine Geschäftslogik in der UI-Schicht —
  Dashboard und Desktop rufen dieselben Python-Funktionen auf wie die CLI.
- **Fenster schliessen darf keinen laufenden Testlauf abbrechen.** Bei aktivem Lauf wird das
  Fenster nur versteckt.
- **Eigenes Symboldesign**, keine AVM-Marken oder -Logos.
- mypy ist **strict** für `core/` und `storage/`. ruff und mypy müssen ohne Befund bleiben.

## Fallstricke

Das hier hat schon Zeit gekostet:

- **SQLite läuft im WAL-Modus.** Wer die Datenbank kopiert, muss `fbtest.sqlite-wal` und
  `-shm` mitkopieren — sonst fehlen genau die neuesten Zeilen, und die Ursache ist von aussen
  nicht erkennbar.
- **Wiederholte Subprozesse brauchen `hidden_process_kwargs()`** aus `proc.py`. Ohne das
  blitzt unter Windows aus der fensterlosen Anwendung bei jedem `netsh`- oder `ping`-Aufruf
  kurz eine Konsole auf — über 24 Stunden mehrere Tausend Mal. `test_proc.py` prüft auf
  Quelltextebene, dass jede wiederkehrende Aufrufstelle das mitgibt.
- **PyInstaller ohne `--clean`** liefert unter Umständen die alte Oberfläche mit. Der Fehler
  sieht aus wie eine wirkungslose Korrektur, und man sucht dann im Code statt im Bauvorgang.
  `build.ps1` vergleicht deshalb die Prüfsummen der sechs mitgelieferten Dateien.
- **Reine Python-Pakete landen im PYZ-Archiv, nicht als `_internal/<paket>`-Ordner.** Ein
  fehlender Ordner ist also kein Beweis dafür, dass eine Abhängigkeit fehlt — funktional
  prüfen.
- **Metriknamen in `report/generator.py` müssen exakt denen entsprechen, die die Module
  veröffentlichen.** Ein Tippfehler erzeugt keine Fehlermeldung, sondern ein dauerhaft leeres
  Diagramm. (`loss_pct`, nicht `packet_loss_pct`.) `test_core.py` prüft das auf Quelltextebene.
- **openpyxl: `sheet.freeze_panes` als Zeichenkette setzen** (`f"A{sheet.max_row + 1}"`).
  Der Zugriff über `sheet.cell(...)` legt die Zelle an, und das Blatt hat danach eine leere
  Zeile unter der Kopfzeile.
- **Der Rechner darf während eines Laufs nicht in den Standby.** `power.py` unterdrückt das;
  ein Fehlschlag bricht den Lauf bewusst nicht ab, wird aber als `POWER_KEEPALIVE` festgehalten.
- **Vor einem Neubau prüfen, ob die Anwendung noch läuft** — ein laufender Prozess hält die
  DLLs und PyInstaller scheitert mit `PermissionError [WinError 5]`. Vorher in der Datenbank
  nachsehen, ob gerade ein Testlauf aktiv ist.

## Beim Erweitern

Eine neue Metrik oder ein neues Ereignis berührt mehrere Stellen — reihum prüfen:
`core/models.py` (Enum, Modell) → Modul, das den Wert erzeugt → `storage/database.py`
(Schema, `_TABLE_ORDER`) → `storage/workbook.py` (Blatt/Spalte) → `report/generator.py`
(Auswertung und ggf. Diagramm) → Test → `README.md` (Testzahl und ggf. Tabelle).

Neue Konfigurationsfelder brauchen **keine** Formularpflege: Die Einstellungsmaske wird aus
dem JSON-Schema der pydantic-Modelle erzeugt. Ein neues Feld in `config.py` mit `description`
erscheint dort automatisch.
