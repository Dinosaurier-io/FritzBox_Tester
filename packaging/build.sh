#!/usr/bin/env bash
# Baut das Linux-Programmpaket.
#
# Aufruf aus dem Projektordner:
#   ./packaging/build.sh
#
# Hinweis: Das Ergebnis ist an die glibc-Version des Baurechners gebunden.
# Fuer breite Lauffaehigkeit auf der aeltesten noch unterstuetzten Distribution
# bauen - ein auf Ubuntu 24.04 gebautes Paket laeuft nicht auf Ubuntu 22.04.
#
# Ein natives Fenster gibt es hier bewusst nicht: pywebview braucht dafuer
# systemweit installierte GTK-/WebKit-Bibliotheken, die sich nicht verlaesslich
# mitliefern lassen. Die Anwendung faellt automatisch auf ein Browserfenster
# im App-Modus zurueck.

set -euo pipefail
project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project"

python="${project}/.venv/bin/python"
[ -x "$python" ] || { echo "Virtuelle Umgebung fehlt: $python" >&2; exit 1; }

echo "== Qualitaetspruefung =="
"$python" -m pytest -q
"$python" -m ruff check .
"$python" -m mypy

echo
echo "== Symbole erzeugen =="
"$python" -c "from pathlib import Path
from fbtest.desktop.application import build_icons
print(len(build_icons(Path('assets'))), 'Dateien')"

echo
echo "== Programmpaket bauen =="
rm -rf build dist
"$python" -m PyInstaller packaging/fbtest.spec --noconfirm --clean

target="${project}/dist/FRITZBox-Langzeittest"

# Gegenprobe: Ein Neubau ohne --clean liefert unter Umstaenden die alte
# Oberflaeche mit. Nichts ist aergerlicher als eine Korrektur, die im Paket
# fehlt - man sucht den Fehler dann im Code statt im Bauvorgang.
echo
echo "== Mitgelieferte Dateien pruefen =="
for pair in \
    "src/fbtest/dashboard/static/index.html:_internal/fbtest/dashboard/static/index.html" \
    "src/fbtest/dashboard/static/app.js:_internal/fbtest/dashboard/static/app.js" \
    "src/fbtest/dashboard/static/style.css:_internal/fbtest/dashboard/static/style.css" \
    "src/fbtest/dashboard/static/settings.js:_internal/fbtest/dashboard/static/settings.js" \
    "src/fbtest/dashboard/static/setup.js:_internal/fbtest/dashboard/static/setup.js" \
    "src/fbtest/resources/config.example.yaml:_internal/fbtest/resources/config.example.yaml"
do
    src="${project}/${pair%%:*}"
    dst="${target}/${pair##*:}"
    if ! cmp -s "$src" "$dst"; then
        echo "Veraltete oder fehlende Fassung im Paket: ${pair##*:}" >&2
        echo "Mit --clean neu bauen." >&2
        exit 1
    fi
    echo "  aktuell: ${pair##*:}"
done

echo
echo "== Fertig =="
echo "Ordner : $target"
echo "Groesse: $(du -sh "$target" | cut -f1)"

# Startdatei fuer das Anwendungsmenue.
cat > "${target}/fbtest.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FRITZ!Box-Langzeittest
Comment=Langzeit-Stabilitaetstest fuer FRITZ!Box-Router
Exec=${target}/FRITZBox-Langzeittest
Icon=${project}/assets/fbtest-256.png
Terminal=false
Categories=Network;Utility;
EOF

echo "Startdatei: ${target}/fbtest.desktop"
echo "Installieren mit: cp '${target}/fbtest.desktop' ~/.local/share/applications/"
