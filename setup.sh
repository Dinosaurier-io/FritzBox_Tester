#!/usr/bin/env bash
# Richtet das Projekt auf einem frischen Linux-Rechner ein.
#
# Aufruf im Projektordner:
#   ./setup.sh              # Laufzeit + Entwicklungswerkzeuge
#   ./setup.sh --minimal    # nur Laufzeit, ohne pytest/ruff/mypy/PyInstaller
#   ./setup.sh --no-test    # ueberspringt die Testsuite am Ende
#
# Alle Pfade sind relativ zum Skript. Es ist also egal, wohin das Repository
# geklont wurde und wie der Benutzer heisst.

set -euo pipefail
project="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project"

minimal=0
run_tests=1
for arg in "$@"; do
    case "$arg" in
        --minimal) minimal=1 ;;
        --no-test) run_tests=0 ;;
        *) echo "Unbekannte Option: $arg" >&2; exit 2 ;;
    esac
done

schritt() { printf '\n== %s ==\n' "$1"; }
gut() { printf '   %s\n' "$1"; }

# --- 1. Python finden -------------------------------------------------------
schritt "Python suchen"
python=""
for kandidat in python3.14 python3 python; do
    command -v "$kandidat" >/dev/null 2>&1 || continue
    if "$kandidat" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)' 2>/dev/null; then
        python="$(command -v "$kandidat")"
        break
    fi
    gefunden="$("$kandidat" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
    printf '   uebersprungen: %s (Python %s)\n' "$kandidat" "$gefunden"
done

if [ -z "$python" ]; then
    cat >&2 <<'EOF'
Python 3.14 oder neuer wurde nicht gefunden.

Debian/Ubuntu:  sudo apt install python3.14 python3.14-venv
Fedora:         sudo dnf install python3.14
Alternativ:     https://github.com/astral-sh/uv  oder  pyenv
EOF
    exit 1
fi
gut "gefunden: $python ($("$python" -V))"

# --- 2. Virtuelle Umgebung --------------------------------------------------
schritt "Virtuelle Umgebung anlegen"
venv_python="$project/.venv/bin/python"
if [ -x "$venv_python" ]; then
    gut "vorhanden, wird weiterverwendet: .venv"
else
    "$python" -m venv "$project/.venv"
    gut "angelegt: .venv"
fi
"$venv_python" -m pip install --quiet --upgrade pip

# --- 3. Abhaengigkeiten -----------------------------------------------------
anforderungen="requirements-dev.txt"
[ "$minimal" -eq 1 ] && anforderungen="requirements.txt"
schritt "Abhaengigkeiten installieren ($anforderungen)"
"$venv_python" -m pip install --quiet -r "$project/$anforderungen"
gut "installiert"

schritt "Projekt einbinden"
# Editierbar, damit Aenderungen am Quellcode ohne erneute Installation wirken.
"$venv_python" -m pip install --quiet -e "$project"
gut "die Befehle 'fbtest' und 'fbtest-app' stehen in der Umgebung bereit"

# --- 4. Gegenprobe ----------------------------------------------------------
schritt "Gegenprobe"
fehlend="$("$venv_python" - <<'PY'
import importlib.util as u
pakete = ['pydantic', 'yaml', 'ruamel.yaml', 'keyring', 'typer', 'rich', 'httpx',
          'icmplib', 'fritzconnection', 'jinja2', 'openpyxl', 'matplotlib',
          'fastapi', 'uvicorn', 'pystray', 'PIL', 'fbtest']
print(' '.join(p for p in pakete if u.find_spec(p) is None))
PY
)"
# pywebview steht bewusst nicht in der Liste: Unter Linux braucht es GTK- und
# WebKit-Bibliotheken vom System. Fehlen sie, faellt die Anwendung auf ein
# Browserfenster zurueck - das ist kein Fehler, sondern der vorgesehene Weg.
if [ -n "$fehlend" ]; then
    echo "Nicht importierbar: $fehlend" >&2
    exit 1
fi
gut "alle Pakete importierbar"

"$venv_python" -m fbtest version

if [ "$minimal" -eq 0 ] && [ "$run_tests" -eq 1 ]; then
    schritt "Testsuite"
    "$venv_python" -m pytest
fi

# --- 5. Wie es weitergeht ---------------------------------------------------
cat <<'EOF'

== Fertig ==

Naechste Schritte:

  source .venv/bin/activate     Umgebung aktivieren
  fbtest init                   Ordner und config.yaml anlegen
  fbtest check                  Voraussetzungen pruefen (braucht die FRITZ!Box)
  fbtest dashboard              Dashboard im Browser oeffnen
  fbtest run --duration 10m     Testlauf starten

Passwort der FRITZ!Box hinterlegen (nie in die config.yaml eintragen):

  export FRITZ_PASSWORD='dein-passwort'

Optional fuer praezise ICMP-Messung ohne Administratorrechte:

  sudo setcap cap_net_raw+ep "$(readlink -f .venv/bin/python)"
EOF
