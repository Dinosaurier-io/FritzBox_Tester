# Richtet das Projekt auf einem frischen Windows-Rechner ein.
#
# Aufruf im Projektordner:
#   .\setup.ps1              # Laufzeit + Entwicklungswerkzeuge
#   .\setup.ps1 -Minimal     # nur Laufzeit, ohne pytest/ruff/mypy/PyInstaller
#   .\setup.ps1 -NoTest      # ueberspringt die Testsuite am Ende
#
# Das Skript arbeitet ausschliesslich mit Pfaden relativ zu sich selbst. Es ist
# also egal, wohin das Repository geklont wurde und wie der Benutzer heisst.

[CmdletBinding()]
param(
    [switch]$Minimal,
    [switch]$NoTest
)

$ErrorActionPreference = "Stop"
$project = $PSScriptRoot
Set-Location $project

function Schritt($text) { Write-Host "`n== $text ==" -ForegroundColor Cyan }
function Gut($text) { Write-Host "   $text" -ForegroundColor Green }

# --- 1. Python finden -------------------------------------------------------
# Der Launcher "py" kennt auch Installationen, die nicht im PATH stehen. Das
# ist unter Windows der Normalfall, wenn man beim Installieren den Haken
# vergessen hat.
Schritt "Python suchen"
$python = $null
foreach ($kandidat in @(
        @{ exe = "py"; args = @("-3.14", "-c", "import sys; print(sys.executable)") },
        @{ exe = "py"; args = @("-3", "-c", "import sys; print(sys.executable)") },
        @{ exe = "python"; args = @("-c", "import sys; print(sys.executable)") })) {
    if (-not (Get-Command $kandidat.exe -ErrorAction SilentlyContinue)) { continue }
    try { $gefunden = & $kandidat.exe @($kandidat.args) 2>$null } catch { continue }
    if ($LASTEXITCODE -ne 0 -or -not $gefunden) { continue }
    $version = & $gefunden -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ([version]$version -ge [version]"3.14") { $python = $gefunden; break }
    Write-Host "   uebersprungen: $gefunden (Python $version)" -ForegroundColor DarkGray
}

if (-not $python) {
    Write-Error @"
Python 3.14 oder neuer wurde nicht gefunden.

Herunterladen: https://www.python.org/downloads/
Beim Installieren "Add python.exe to PATH" ankreuzen, danach dieses Fenster
schliessen, ein neues oeffnen und '.\setup.ps1' erneut aufrufen.
"@
}
Gut "gefunden: $python"

# --- 2. Virtuelle Umgebung --------------------------------------------------
Schritt "Virtuelle Umgebung anlegen"
$venv = Join-Path $project ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (Test-Path $venvPython) {
    Gut "vorhanden, wird weiterverwendet: .venv"
} else {
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Error "Anlegen der virtuellen Umgebung fehlgeschlagen." }
    Gut "angelegt: .venv"
}

& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Error "pip liess sich nicht aktualisieren." }

# --- 3. Abhaengigkeiten -----------------------------------------------------
$anforderungen = if ($Minimal) { "requirements.txt" } else { "requirements-dev.txt" }
Schritt "Abhaengigkeiten installieren ($anforderungen)"
& $venvPython -m pip install --quiet -r (Join-Path $project $anforderungen)
if ($LASTEXITCODE -ne 0) { Write-Error "Installation der Abhaengigkeiten fehlgeschlagen." }
Gut "installiert"

Schritt "Projekt einbinden"
# Editierbar, damit Aenderungen am Quellcode ohne erneute Installation wirken.
& $venvPython -m pip install --quiet -e $project
if ($LASTEXITCODE -ne 0) { Write-Error "Einbinden des Projekts fehlgeschlagen." }
Gut "die Befehle 'fbtest' und 'fbtest-app' stehen in der Umgebung bereit"

# --- 4. Gegenprobe ----------------------------------------------------------
Schritt "Gegenprobe"
$fehlend = & $venvPython -c @"
import importlib.util as u
pakete = ['pydantic', 'yaml', 'ruamel.yaml', 'keyring', 'typer', 'rich', 'httpx',
          'icmplib', 'fritzconnection', 'jinja2', 'openpyxl', 'matplotlib',
          'fastapi', 'uvicorn', 'webview', 'pystray', 'PIL', 'fbtest']
print(' '.join(p for p in pakete if u.find_spec(p) is None))
"@
if ($fehlend) { Write-Error "Nicht importierbar: $fehlend" }
Gut "alle Pakete importierbar"

& $venvPython -m fbtest version
if ($LASTEXITCODE -ne 0) { Write-Error "'fbtest version' laeuft nicht." }

if (-not $Minimal -and -not $NoTest) {
    Schritt "Testsuite"
    & $venvPython -m pytest
    if ($LASTEXITCODE -ne 0) { Write-Error "Die Testsuite ist nicht gruen." }
}

# --- 5. Wie es weitergeht ---------------------------------------------------
Write-Host "`n== Fertig ==" -ForegroundColor Green
Write-Host @"

Naechste Schritte:

  .\.venv\Scripts\Activate.ps1     Umgebung aktivieren
  fbtest init                      Ordner und config.yaml anlegen
  fbtest check                     Voraussetzungen pruefen (braucht die FRITZ!Box)
  fbtest app                       Fenster oeffnen
  fbtest run --duration 10m        Testlauf starten

Passwort der FRITZ!Box hinterlegen (nie in die config.yaml eintragen):

  `$env:FRITZ_PASSWORD = 'dein-passwort'

oder dauerhaft im Schluesselspeicher ueber die Einstellungen im Fenster.
"@ -ForegroundColor Gray
