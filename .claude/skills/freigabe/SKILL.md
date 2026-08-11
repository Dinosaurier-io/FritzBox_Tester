---
name: freigabe
description: Prüft und baut das Programmpaket freigabereif - ruff, mypy, pytest, PyInstaller-Neubau samt Prüfsummenkontrolle der mitgelieferten Oberflächendateien. Verwenden, wenn ein Paket gebaut, eine Änderung freigegeben oder "die exe neu gebaut" werden soll, sowie zur Endkontrolle nach grösseren Änderungen.
---

# Freigabe: prüfen und bauen

Ziel ist ein Paket in `dist\FRITZBox-Langzeittest\`, bei dem nachweislich alles drin ist,
was im Quellcode steht. Die Reihenfolge unten ist nicht beliebig — jeder Schritt verhindert
einen Fehler, der schon einmal aufgetreten ist.

## 1. Vorher: läuft die Anwendung noch?

PyInstaller scheitert mit `PermissionError [WinError 5]`, wenn ein laufender Prozess die
DLLs im Zielordner hält. Also zuerst nachsehen:

```powershell
Get-Process FRITZBox-Langzeittest, fbtest -ErrorAction SilentlyContinue
```

Läuft etwas, **nicht einfach beenden**. Zuerst prüfen, ob gerade ein Testlauf aktiv ist —
ein abgewürgter Lauf über mehrere Stunden ist nicht wiederherstellbar:

```powershell
.\.venv\Scripts\python.exe -c @'
from pathlib import Path
from fbtest.storage.database import Database
db_path = Path.home() / "AppData/Local/fbtest/data/fbtest.sqlite"
with Database(db_path) as db:
    print(db.get_open_test_run())
'@
```

Kommt `None` zurück, darf der Prozess beendet werden. Kommt ein Lauf zurück: **abbrechen und
den Benutzer fragen.** Nie eigenmächtig eine laufende Messung beenden.

## 2. Bauen

```powershell
$out = & .\packaging\build.ps1 2>&1
$out | Select-Object -Last 30
```

**Die Ausgabe zuerst vollständig in eine Variable schreiben.** Ein direktes
`.\packaging\build.ps1 | Select-Object -First 25` schliesst die Pipeline, sobald genug Zeilen
da sind — PyInstaller wird dabei mitten im Lauf abgewürgt und `dist\` bleibt leer. Der Fehler
sieht aus wie ein Build-Problem, ist aber eines der Ausgabeumleitung.

Das Skript erledigt in dieser Reihenfolge selbst:

1. `pytest` → `ruff check` → `mypy`; bricht bei jedem Befund ab, damit gar kein Paket aus
   fehlerhaftem Stand entsteht
2. Symbole erzeugen
3. `build\` und `dist\` löschen, PyInstaller mit `--clean`
4. Prüfsummen der sechs mitgelieferten Dateien gegen den Quellstand vergleichen

Schritt 4 ist der wichtigste. Ein Neubau ohne `--clean` übernimmt sonst die
zwischengespeicherte alte Oberfläche — die Korrektur wirkt dann scheinbar nicht, und man sucht
im Quelltext statt im Bauvorgang. Kommt hier `Veraltete Fassung im Paket`, ist das kein
Fehlalarm.

## 3. Rauchtest

```powershell
& '.\dist\FRITZBox-Langzeittest\FRITZBox-Langzeittest.exe' version
```

Bei Änderungen an Bibliotheken zusätzlich die betroffene Funktion aus dem **gebündelten**
Programm heraus aufrufen, nicht aus dem Quellcode. Reine Python-Pakete landen im PYZ-Archiv
und **nicht** als `_internal/<paket>`-Ordner — ein fehlender Ordner beweist also nichts. Nur
der funktionale Aufruf beweist etwas. Beispiel für den Excel-Export:

```powershell
& '.\dist\FRITZBox-Langzeittest\FRITZBox-Langzeittest.exe' export 3 --format xlsx
```

## 4. Melden

Kurz und mit Zahlen: Testanzahl, ruff/mypy-Ergebnis, Paketgrösse, welche Rauchtests gelaufen
sind. Wenn etwas übersprungen wurde, das ausdrücklich sagen.

Hat sich die **Testanzahl** geändert, in `README.md` nachziehen — sie steht an zwei Stellen
(Ordnerstruktur und Abschnitt „Entwicklung und Qualitätssicherung"). Neue Testdateien
gehören zusätzlich in die Tabelle darunter.
