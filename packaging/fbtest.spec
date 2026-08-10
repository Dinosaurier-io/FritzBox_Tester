# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-Beschreibung des Programmpakets.

Erzeugt **zwei** Programme in einem gemeinsamen Ordner:

* ``FRITZBox-Langzeittest`` - ohne Konsolenfenster, oeffnet Fenster und
  Infobereich-Symbol. Das ist das Programm zum Doppelklicken.
* ``fbtest`` - mit Konsole, die vollstaendige Kommandozeile. Sie bleibt
  erhalten, damit sich Testlaeufe weiterhin automatisieren lassen.

Beide teilen sich die Bibliotheken; das Paket bleibt dadurch nur unwesentlich
groesser als mit einem einzigen Programm.

Bewusst **onedir** und nicht onefile: onefile entpackt bei jedem Start rund
80 MB in den Temporaerordner. Mit matplotlib im Gepaeck sind das mehrere
Sekunden Wartezeit vor jedem Fensteroeffnen - bei einem Programm, das man
mehrmals taeglich startet, ist das der falsche Kompromiss.

Aufruf aus dem Projektordner::

    pyinstaller packaging/fbtest.spec --noconfirm
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT = Path(SPECPATH).parent
PACKAGE = PROJECT / "src" / "fbtest"

# --------------------------------------------------------------------------
# Mitgelieferte Dateien
# --------------------------------------------------------------------------
# Alle drei Ordner werden zur Laufzeit ueber fbtest.paths.resource_path()
# gefunden. Die Zielpfade muessen deshalb der Paketstruktur entsprechen.
datas = [
    (str(PACKAGE / "dashboard" / "static"), "fbtest/dashboard/static"),
    (str(PACKAGE / "report" / "templates"), "fbtest/report/templates"),
    (str(PACKAGE / "resources"), "fbtest/resources"),
]
datas += collect_data_files("fritzconnection")

# --------------------------------------------------------------------------
# Versteckte Importe
# --------------------------------------------------------------------------
hiddenimports = [
    # uvicorn laedt seine Protokoll-Umsetzungen ueber Zeichenketten; der
    # Abhaengigkeitsscanner von PyInstaller sieht sie deshalb nicht.
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
    # keyring waehlt seinen Speicher zur Laufzeit aus.
    *collect_submodules("keyring.backends"),
    # pystray und pywebview ebenso ihre Plattformumsetzung.
    *collect_submodules("pystray"),
    *collect_submodules("webview.platforms"),
    "PIL._tkinter_finder",
]

# --------------------------------------------------------------------------
# Ausgeschlossen
# --------------------------------------------------------------------------
# matplotlib wird ausschliesslich mit dem Agg-Backend benutzt (siehe
# report/charts.py). Die grafischen Backends zoegen komplette GUI-Werkzeugkaesten
# ins Paket - zweistellige Megabyte fuer Code, der nie ausgefuehrt wird.
excludes = [
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
    "matplotlib.backends.backend_qt5agg",
    "matplotlib.backends.backend_tkagg",
    "IPython",
    "pytest",
    "notebook",
]

block_cipher = None
ICON = str(PROJECT / "assets" / "fbtest.ico")


def analysis(entry: str) -> Analysis:  # noqa: F821 - von PyInstaller bereitgestellt
    """Baut die Abhaengigkeitsanalyse fuer einen Einstiegspunkt."""
    return Analysis(  # noqa: F821
        [str(PROJECT / "packaging" / entry)],
        pathex=[str(PROJECT / "src")],
        binaries=[],
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        runtime_hooks=[str(PROJECT / "packaging" / "runtime_hook.py")],
        excludes=excludes,
        noarchive=False,
    )


gui_analysis = analysis("entry_gui.py")
cli_analysis = analysis("entry_cli.py")

MERGE((gui_analysis, "gui", "gui"), (cli_analysis, "cli", "cli"))  # noqa: F821

gui_pyz = PYZ(gui_analysis.pure)  # noqa: F821
cli_pyz = PYZ(cli_analysis.pure)  # noqa: F821

gui_exe = EXE(  # noqa: F821
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="FRITZBox-Langzeittest",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # kein Konsolenfenster
    icon=ICON,
)

cli_exe = EXE(  # noqa: F821
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="fbtest",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # die Kommandozeile braucht eine
    icon=ICON,
)

COLLECT(  # noqa: F821
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="FRITZBox-Langzeittest",
)
