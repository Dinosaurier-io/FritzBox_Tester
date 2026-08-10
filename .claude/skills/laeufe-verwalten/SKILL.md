---
name: laeufe-verwalten
description: Testläufe in der Datenbank auflisten, löschen, abschliessen oder aufräumen - samt der zugehörigen Berichte und Tabellen im Dateisystem. Verwenden, wenn ein Lauf gelöscht, ein abgebrochener Lauf abgeschlossen oder Ordnung in der Laufliste geschaffen werden soll.
---

# Testläufe verwalten

Für Löschen und Abschliessen gibt es **keinen CLI-Befehl** — nur das Dashboard
(Reiter *Testläufe*) und die Speicher-Schnittstelle. Von hier aus also über die
Schnittstelle, nicht mit rohem SQL: `delete_test_run` räumt alle sechs Untertabellen mit ab,
ein `DELETE FROM test_runs` liesse verwaiste Zeilen zurück.

## Vor jedem Löschen

**Erst zeigen, dann löschen.** Immer ausgeben, was verschwinden würde, und die Bestätigung
des Benutzers abwarten, wenn er nicht schon eine konkrete Nummer genannt hat. Eine gelöschte
Messreihe über 72 Stunden ist nicht wiederherstellbar.

**Nie einen laufenden Testlauf löschen.** Das Dashboard blockt das mit HTTP 409 ab — die
Speicher-Schnittstelle tut das **nicht**, dort gibt es keinen Schutz. Also selbst prüfen:

```powershell
.\.venv\Scripts\python.exe -c @'
from pathlib import Path
from fbtest.storage.database import Database
db_path = Path.home() / "AppData/Local/fbtest/data/fbtest.sqlite"
with Database(db_path) as db:
    print("Offen:", db.get_open_test_run())
    for run in db.list_test_runs():
        print(run.id, repr(run.name), f"{run.duration_s:.0f} s", "offen" if run.ended_at is None else "")
'@
```

Läuft die Desktop-Anwendung, hält sie ihre eigene Verbindung. Löschen ist wegen WAL trotzdem
möglich, aber die Oberfläche zeigt den Lauf bis zur nächsten Aktualisierung weiter an — das
ist kein Fehler.

## Löschen

```powershell
.\.venv\Scripts\python.exe -c @'
from pathlib import Path
from fbtest.storage.database import Database

db_path = Path.home() / "AppData/Local/fbtest/data/fbtest.sqlite"
with Database(db_path) as db:
    run = db.get_test_run(8)
    print("Zu loeschen:", run.id, repr(run.name), f"{run.duration_s:.0f} s")
    print("Geloeschte Zeilen:", db.delete_test_run(8))
    print("Verbleibend:", [(r.id, r.name) for r in db.list_test_runs()])
'@
```

Die zurückgegebene Zahl sind die Datenzeilen aus allen Untertabellen — sie taugt als Beleg,
dass tatsächlich etwas weg ist.

## Dateien im Dateisystem

`delete_test_run` löscht **nur** die Datenbankzeilen. Bericht und Tabelle bleiben liegen:

- `%LOCALAPPDATA%\fbtest\exports\` — `<Laufname>.xlsx`, benannt nach dem Lauf (Sonderzeichen
  ersetzt), bei Namensgleichheit mit dem Zusatz ` (Lauf N)`; dazu `.csv`/`.json` aus älteren
  Exporten
- `%LOCALAPPDATA%\fbtest\reports\` — `bericht_run<NNNN>.html`, also nach Nummer benannt und
  damit auch nach dem Löschen noch eindeutig zuzuordnen

Ob sie mit weg sollen, den Benutzer entscheiden lassen und die betroffenen Dateien namentlich
nennen. Ein Bericht ist unter Umständen genau das, was von einem Lauf aufgehoben werden soll.

## Abgebrochene Läufe

Ein Lauf ohne `ended_at` ist entweder aktiv oder abgestürzt. Unterscheiden über
`db.last_activity(run_id)` — liegt der letzte Messwert lange zurück, war es ein Absturz.

- **Als beendet markieren:** `db.finish_test_run(run_id)` setzt `ended_at`.
  `db.close_dangling_outages(run_id)` schliesst zusätzlich Ausfälle, die durch den Absturz nie
  ein Ende bekommen haben — ohne das erscheint im Bericht ein Ausfall, der bis heute andauert.
- **Fortsetzen** gehört ins Dashboard bzw. `fbtest run`: Dort erscheint der Dialog mit den
  Zahlen, die für die Entscheidung nötig sind (Messwerte, Ausfälle, Länge der Lücke). Zwei
  Messreihen mit Tagen dazwischen zusammenzukleben ergibt keinen auswertbaren Lauf.

## Aufräumen

`db.vacuum_check()` meldet den zurückgewinnbaren Platz. Nach dem Löschen grosser Läufe lohnt
sich `VACUUM`, sonst bleibt die Datei so gross wie vorher.

Beim Melden konkret sein: welche Nummer, welcher Name, wie viele Zeilen, was im Dateisystem
angefasst wurde und was nicht.
