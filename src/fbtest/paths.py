"""Auflösung von Ressourcen- und Arbeitsverzeichnissen.

Das Programm laeuft in drei sehr unterschiedlichen Situationen, die sich in der
Pfadfrage widersprechen:

* **Aus dem Quellcode** (Entwicklung): Alles liegt im Projektordner, ``data/``
  und ``logs/`` sollen dort entstehen und nirgends sonst.
* **Als installiertes Paket** (``pip install fbtest``): Der Nutzer startet das
  Programm irgendwo; ein Arbeitsverzeichnis im Paketordner waere falsch.
* **Als gebuendelte Anwendung** (PyInstaller): Das Programmverzeichnis ist
  schreibgeschuetzt - ``data/`` daneben anzulegen schlaegt fehl oder landet in
  ``C:\\Windows\\System32``, je nachdem, von wo aus gestartet wurde.

Dieses Modul beantwortet deshalb zwei getrennte Fragen:

* :func:`resource_path` - **Wo liegen mitgelieferte Dateien?** (Vorlagen,
  Web-Oberflaeche, Beispielkonfiguration). Nur lesend.
* :func:`resolve_paths` - **Wohin schreibt das Programm?** (Konfiguration,
  Datenbank, Protokolle, Berichte, Exporte).

Beide Funktionen sind bewusst ohne Seiteneffekte und mit einspeisbarer Umgebung
gebaut, damit sie ohne echtes Dateisystem und ohne echtes Bundle testbar sind.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: Ordnername unterhalb der plattformueblichen Nutzerverzeichnisse.
APP_DIR_NAME = "fbtest"

#: Name der Konfigurationsdatei.
CONFIG_NAME = "config.yaml"

#: Name der mitgelieferten Vorlage.
EXAMPLE_NAME = "config.example.yaml"

#: Umgebungsvariable, die alle automatischen Regeln uebersteuert.
HOME_ENV = "FBTEST_HOME"

#: Merkmale, an denen der Quellcode-Projektordner erkennbar ist. Bewusst beide
#: zusammen: Ein ``pyproject.toml`` allein steht in jedem beliebigen
#: Python-Projekt und wuerde dort faelschlich eine ``config.yaml`` anlegen.
_PROJECT_FILE = "pyproject.toml"
_PROJECT_PACKAGE = Path("src") / APP_DIR_NAME


# --------------------------------------------------------------------------
# Mitgelieferte Ressourcen
# --------------------------------------------------------------------------


def is_frozen() -> bool:
    """Meldet, ob das Programm als gebuendelte Anwendung laeuft."""
    return getattr(sys, "frozen", False)


def bundle_dir() -> Path:
    """Wurzel der mitgelieferten Paketdateien.

    Im Bundle entpackt PyInstaller die Paketdaten nach ``sys._MEIPASS``; die
    Ordnerstruktur unterhalb von ``fbtest/`` bleibt dabei erhalten. Ohne Bundle
    ist es schlicht der Paketordner.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is not None:
        return Path(base) / APP_DIR_NAME
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """Pfad einer mitgelieferten Datei innerhalb des Pakets.

    Args:
        *parts: Pfadbestandteile relativ zum Paketordner,
            z.B. ``("report", "templates")``.

    Returns:
        Absoluter Pfad. Ob die Datei existiert, wird nicht geprueft - das ist
        Sache des Aufrufers, der den Fehler sinnvoller melden kann.
    """
    return bundle_dir().joinpath(*parts)


def example_config_path() -> Path:
    """Pfad der mitgelieferten, kommentierten Beispielkonfiguration."""
    return resource_path("resources", EXAMPLE_NAME)


# --------------------------------------------------------------------------
# Arbeitsverzeichnisse
# --------------------------------------------------------------------------


class PathOrigin(StrEnum):
    """Regel, nach der die Arbeitsverzeichnisse bestimmt wurden.

    Wird in der Diagnose angezeigt: Die haeufigste Verwirrung bei einem
    Werkzeug mit mehreren Betriebsarten ist "warum sind meine alten Testlaeufe
    weg?" - meist, weil eine andere Regel gegriffen hat als erwartet.
    """

    EXPLICIT = "explicit"
    """Ausdrueckliche Angabe ueber ``--config``."""

    ENVIRONMENT = "environment"
    """Ueber die Umgebungsvariable ``FBTEST_HOME``."""

    WORKING_DIR = "working_dir"
    """Vorhandene ``config.yaml`` im Arbeitsverzeichnis."""

    PROJECT_DIR = "project_dir"
    """Quellcode-Projektordner (nur ohne Bundle)."""

    USER_DIR = "user_dir"
    """Plattformuebliches Nutzerverzeichnis."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Ergebnis der Pfadauflösung."""

    config_file: Path
    """Vollstaendiger Pfad der ``config.yaml``."""

    base_dir: Path
    """Basis fuer relative Pfade aus ``storage.*`` (``data/``, ``logs/`` ...)."""

    origin: PathOrigin
    """Welche Regel gegriffen hat."""

    @property
    def exists(self) -> bool:
        """True, wenn bereits eine Konfigurationsdatei vorliegt."""
        return self.config_file.is_file()

    def describe(self) -> str:
        """Erklaert in einem Satz, warum diese Pfade gewaehlt wurden."""
        reasons = {
            PathOrigin.EXPLICIT: "ausdruecklich mit --config angegeben",
            PathOrigin.ENVIRONMENT: f"ueber die Umgebungsvariable {HOME_ENV} vorgegeben",
            PathOrigin.WORKING_DIR: "im aktuellen Arbeitsverzeichnis gefunden",
            PathOrigin.PROJECT_DIR: "Projektordner des Quellcodes",
            PathOrigin.USER_DIR: "plattformuebliches Benutzerverzeichnis",
        }
        return reasons[self.origin]


def user_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Plattformueblicher Ort fuer Konfigurationsdateien."""
    env = os.environ if environ is None else environ

    if sys.platform == "win32":
        # Bewusst LOCALAPPDATA und nicht APPDATA: In Firmenumgebungen wird
        # APPDATA auf einen Server gespiegelt. Eine Messdatenbank, die ueber
        # Tage auf mehrere hundert MB waechst, hat dort nichts verloren -
        # und Konfiguration und Daten zu trennen wuerde nur verwirren.
        root = env.get("LOCALAPPDATA") or env.get("APPDATA")
        return Path(root) / APP_DIR_NAME if root else Path.home() / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME

    root = env.get("XDG_CONFIG_HOME")
    return (Path(root) if root else Path.home() / ".config") / APP_DIR_NAME


def user_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Plattformueblicher Ort fuer Messdaten, Protokolle und Berichte."""
    env = os.environ if environ is None else environ

    if sys.platform == "win32" or sys.platform == "darwin":
        # Unter Windows und macOS ist die Trennung von Konfiguration und Daten
        # nicht ueblich - ein Ordner ist dort das erwartete Verhalten.
        return user_config_dir(env)

    root = env.get("XDG_DATA_HOME")
    return (Path(root) if root else Path.home() / ".local" / "share") / APP_DIR_NAME


def looks_like_project(directory: Path) -> bool:
    """Prueft, ob ein Ordner der Quellcode-Projektordner von fbtest ist."""
    return (directory / _PROJECT_FILE).is_file() and (directory / _PROJECT_PACKAGE).is_dir()


def resolve_paths(
    config_option: Path | None = None,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    frozen: bool | None = None,
) -> AppPaths:
    """Bestimmt Konfigurationsdatei und Arbeitsverzeichnis.

    Die Regeln greifen in dieser Reihenfolge:

    1. ``--config`` wurde angegeben - der Ordner dieser Datei ist die Basis.
    2. ``FBTEST_HOME`` ist gesetzt - dieser Ordner ist die Basis.
    3. Im Arbeitsverzeichnis liegt bereits eine ``config.yaml``.
    4. Das Arbeitsverzeichnis ist der Projektordner des Quellcodes (nur ohne
       Bundle) - damit ``fbtest init`` im frisch geklonten Projekt dort und
       nicht im Benutzerverzeichnis landet.
    5. Plattformuebliches Benutzerverzeichnis.

    Regel 3 ist die wichtigste: Sie sorgt dafuer, dass eine bestehende
    Installation mit ihrer Datenbank weiterarbeitet, auch nachdem die
    Benutzerverzeichnisse eingefuehrt wurden.

    Args:
        config_option: Wert von ``--config``; ``None`` = nicht angegeben.
        cwd: Arbeitsverzeichnis; ``None`` = aktuelles. Nur fuer Tests.
        environ: Umgebungsvariablen; ``None`` = echte. Nur fuer Tests.
        frozen: Bundle-Betrieb erzwingen bzw. ausschliessen. Nur fuer Tests.

    Returns:
        Die aufgeloesten Pfade samt Begruendung.
    """
    env = os.environ if environ is None else environ
    working = (Path.cwd() if cwd is None else cwd).resolve()
    bundled = is_frozen() if frozen is None else frozen

    if config_option is not None:
        config_file = config_option if config_option.is_absolute() else working / config_option
        return AppPaths(config_file.resolve(), config_file.resolve().parent, PathOrigin.EXPLICIT)

    home = env.get(HOME_ENV, "").strip()
    if home:
        root = Path(home).expanduser().resolve()
        return AppPaths(root / CONFIG_NAME, root, PathOrigin.ENVIRONMENT)

    if (working / CONFIG_NAME).is_file():
        return AppPaths(working / CONFIG_NAME, working, PathOrigin.WORKING_DIR)

    if not bundled and looks_like_project(working):
        return AppPaths(working / CONFIG_NAME, working, PathOrigin.PROJECT_DIR)

    return AppPaths(user_config_dir(env) / CONFIG_NAME, user_data_dir(env), PathOrigin.USER_DIR)
