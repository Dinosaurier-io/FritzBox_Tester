# Baut das Windows-Programmpaket.
#
# Aufruf aus dem Projektordner:
#   .\packaging\build.ps1
#
# Ergebnis: dist\FRITZBox-Langzeittest\  mit beiden Programmen.

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project

$python = Join-Path $project ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Virtuelle Umgebung nicht gefunden: $python`nZuerst 'python -m venv .venv' ausfuehren."
}

Write-Host "== Qualitaetspruefung ==" -ForegroundColor Cyan
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "Tests fehlgeschlagen - es wird nichts gebaut." }
& $python -m ruff check .
if ($LASTEXITCODE -ne 0) { Write-Error "ruff meldet Probleme." }
& $python -m mypy
if ($LASTEXITCODE -ne 0) { Write-Error "mypy meldet Probleme." }

Write-Host "`n== Symbole erzeugen ==" -ForegroundColor Cyan
& $python -c "from pathlib import Path; from fbtest.desktop.application import build_icons; print(len(build_icons(Path('assets'))), 'Dateien')"

Write-Host "`n== Programmpaket bauen ==" -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& $python -m PyInstaller packaging\fbtest.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller fehlgeschlagen." }

$target = Join-Path $project "dist\FRITZBox-Langzeittest"

# Gegenprobe: Ein Neubau ohne --clean liefert unter Umstaenden die alte
# Oberflaeche mit. Nichts ist aergerlicher als eine Korrektur, die im Paket
# fehlt - man sucht den Fehler dann im Code statt im Bauvorgang.
Write-Host "`n== Mitgelieferte Dateien pruefen ==" -ForegroundColor Cyan
$pairs = @(
    @{ src = "src\fbtest\dashboard\static\index.html"; dst = "_internal\fbtest\dashboard\static\index.html" },
    @{ src = "src\fbtest\dashboard\static\app.js";     dst = "_internal\fbtest\dashboard\static\app.js" },
    @{ src = "src\fbtest\dashboard\static\style.css";  dst = "_internal\fbtest\dashboard\static\style.css" },
    @{ src = "src\fbtest\dashboard\static\settings.js"; dst = "_internal\fbtest\dashboard\static\settings.js" },
    @{ src = "src\fbtest\dashboard\static\setup.js";   dst = "_internal\fbtest\dashboard\static\setup.js" },
    @{ src = "src\fbtest\resources\config.example.yaml"; dst = "_internal\fbtest\resources\config.example.yaml" }
)
foreach ($pair in $pairs) {
    $a = Join-Path $project $pair.src
    $b = Join-Path $target $pair.dst
    if (-not (Test-Path $b)) { Write-Error "Im Paket nicht enthalten: $($pair.dst)" }
    if ((Get-FileHash $a).Hash -ne (Get-FileHash $b).Hash) {
        Write-Error "Veraltete Fassung im Paket: $($pair.dst)`nMit --clean neu bauen."
    }
    Write-Host ("  aktuell: {0}" -f $pair.dst)
}

$size = (Get-ChildItem $target -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB

Write-Host "`n== Fertig ==" -ForegroundColor Green
Write-Host ("Ordner : {0}" -f $target)
Write-Host ("Groesse: {0:N0} MB" -f $size)
Write-Host "Programme:"
Get-ChildItem $target -Filter *.exe | ForEach-Object { Write-Host ("  {0}" -f $_.Name) }
Write-Host "`nRauchtest:  & '$target\fbtest.exe' version"
