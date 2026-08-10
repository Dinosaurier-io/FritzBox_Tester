---
name: lauf-auswerten
description: Wertet einen Testlauf aus der Datenbank aus oder stellt zwei Läufe gegenüber (z. B. WLAN gegen LAN, zwei Firmware-Versionen). Verwenden, wenn ein Lauf "angeschaut", analysiert, verglichen oder erklärt werden soll, oder wenn nach den Zahlen eines Laufs gefragt wird.
---

# Testlauf auswerten

## Wo die Daten liegen

Die Datenbank der installierten Anwendung liegt unter
`%LOCALAPPDATA%\fbtest\data\fbtest.sqlite`, aus dem Quellcode heraus unter `data\` im
Projektordner. Welche Regel gegriffen hat, zeigt der Diagnosebereich des Dashboards.

**Nicht kopieren, sondern direkt lesen.** SQLite läuft im WAL-Modus: Eine Kopie der
`.sqlite`-Datei ohne `-wal` und `-shm` enthält genau die neuesten Zeilen nicht — der Lauf
sieht dann leer oder abgeschnitten aus, ohne dass irgendetwas einen Fehler meldet. Muss doch
kopiert werden, alle drei Dateien mitnehmen.

## Auswerten

Nicht von Hand SQL schreiben. `analyze_run` liefert genau die Kennzahlen, die auch im
HTML-Bericht stehen — nur so stimmen Analyse und Bericht überein:

```powershell
.\.venv\Scripts\python.exe -c @'
from pathlib import Path
from fbtest.storage.database import Database
from fbtest.report.generator import analyze_run

db_path = Path.home() / "AppData/Local/fbtest/data/fbtest.sqlite"
with Database(db_path) as db:
    for run in db.list_test_runs():
        print(run.id, repr(run.name), f"{run.duration_s:.0f} s")
    a = analyze_run(db, 7)
    for m in a.summary:
        print(f"{m.label:28} {m.value} {m.unit}")
    print(a.counts)
'@
```

Nützlich daneben: `db.metric_stats(run_id, module, metric)`, `db.percentile(...)`,
`db.list_metric_series(run_id)` (welche Metriken es überhaupt gibt), `db.fetch_events(run_id)`,
`db.fetch_table(tabelle, run_id)`.

Für zwei Läufe: beide einzeln analysieren und gegenüberstellen. `render_comparison(db, a, b,
zielordner)` erzeugt den fertigen HTML-Vergleich, wenn der Benutzer eine Datei möchte.

## Was bei der Bewertung zählt

- **Richtung beachten.** Jede `Metric` trägt `direction` (`lower`/`higher`/`neutral`).
  Datenvolumen ist `neutral` — es hängt an der Laufzeit, nicht an der Qualität.
- **Nur vergleichbare Läufe vergleichen.** Unterschiedliche Dauer macht absolute Zählwerte
  (Anzahl Ausfälle, Neustarts) unbrauchbar; Raten und Mittelwerte bleiben brauchbar.
- **Über welche Verbindung wurde gemessen?** Das Ereignis `NETWORK_PATH` steht am Anfang
  jedes Laufs und nennt Schnittstelle, Gateway und ob mehrere Standardrouten aktiv waren.
  Waren Kabel und WLAN gleichzeitig verbunden, entscheidet das Betriebssystem — ein
  „LAN gegen WLAN"-Vergleich ist dann nicht beweisbar, und das gehört gesagt.
- **Streuung gegen Abstand.** Ein Unterschied zwischen zwei Läufen ist erst dann ein Befund,
  wenn er grösser ist als die Schwankung zwischen Wiederholungen derselben Bedingung. Bei
  drei Minuten Laufzeit ist fast nichts davon belastbar.
- **Wenige Stichproben kennzeichnen.** Ein Speedtest-Mittelwert aus zwei Messungen ist kein
  Mittelwert. Die Anzahl mitnennen statt nur den Wert.
- **Bufferbloat kann negativ sein** — im WLAN hebt der Stromsparmodus (U-APSD) die Latenz im
  *Leerlauf* über die unter Last. Das ist ein echter Effekt, kein Messfehler und kein Anlass,
  die Zahl zu „korrigieren".

## Ausgabe

Deutsch, Tabelle mit den Kennzahlen, darunter die Befunde in Prosa. Nicht jede Zahl
kommentieren, sondern die drei bis fünf, die etwas aussagen. Wenn die Daten eine Frage nicht
beantworten können, das sagen statt zu schätzen.

Bericht (`fbtest report <id>`) oder Tabelle (`fbtest export <id> --format xlsx`) nur erzeugen,
wenn der Benutzer eine Datei haben will — sonst landen Artefakte in seinen Ordnern. Was doch
zum Prüfen erzeugt wurde, hinterher wieder entfernen.
