"""Gemeinsamer Laufzeitkontext von Kommandozeile, Dashboard und Desktop-App.

Bisher wurde die Konfiguration als unveraenderliches Objekt durchgereicht - bei
einem Programm, das seine Einstellungen nur beim Start liest, ist das genau
richtig. Sobald die Einstellungen aber **im Betrieb** aus der Oberflaeche
geaendert werden koennen, faellt dieses Modell auseinander:

* Jede Endpunkt-Funktion des Dashboards haelt eine eigene Referenz auf die
  Konfiguration von damals und wuerde nach dem Speichern weiter mit der alten
  arbeiten.
* Aus ``storage.data_dir`` leitet sich der Pfad der Datenbank ab. Aendert er
  sich, zeigen bereits berechnete Pfade ins Leere.

Der Kontext buendelt deshalb Konfiguration und alle daraus abgeleiteten Pfade
an *einer* Stelle. Alle Beteiligten halten eine Referenz auf den Kontext und
fragen die Werte bei jedem Zugriff neu ab, statt sie zu kopieren.

Bewusst **nicht** enthalten ist der Zustand eines laufenden Testlaufs: Der
gehoert dem :class:`~fbtest.dashboard.controller.RunController`. Ein laufender
Testlauf arbeitet absichtlich mit der Konfiguration weiter, mit der er gestartet
wurde - eine Aenderung mittendrin wuerde die Messreihe unvergleichbar machen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fbtest.config import AppConfig, load_config
from fbtest.paths import AppPaths, example_config_path, resolve_paths


@dataclass(slots=True)
class AppContext:
    """Konfiguration samt aller daraus abgeleiteten Pfade."""

    paths: AppPaths
    """Speicherorte, wie sie beim Start aufgeloest wurden."""

    config: AppConfig
    """Aktuell gueltige Konfiguration. Wird von :meth:`apply` ersetzt."""

    configured: bool = True
    """False, wenn noch keine Konfigurationsdatei existiert.

    Dann enthaelt :attr:`config` die mitgelieferte Vorlage - die Anwendung ist
    lauffaehig, aber noch nicht eingerichtet.
    """

    # -- Erzeugung ---------------------------------------------------------

    @classmethod
    def load(cls, config_option: Path | None = None) -> AppContext:
        """Laedt die Konfiguration am automatisch bestimmten Ort.

        Args:
            config_option: Wert von ``--config``; ``None`` = automatisch suchen.

        Returns:
            Der einsatzbereite Kontext.

        Raises:
            ConfigError: Wenn die Datei fehlt oder ungueltig ist.
        """
        paths = resolve_paths(config_option)
        return cls(paths=paths, config=load_config(paths.config_file))

    @classmethod
    def load_or_template(cls, config_option: Path | None = None) -> AppContext:
        """Wie :meth:`load`, kommt aber ohne vorhandene Datei aus.

        Fuer die Bedienoberflaeche: Sie muss beim allerersten Start starten
        koennen, denn genau dort soll der Einrichtungsassistent laufen. Ein
        Programm, das sich nur einrichten laesst, wenn es bereits eingerichtet
        ist, waere nutzlos.

        Auf der Kommandozeile bleibt es beim harten Fehler - wer ``fbtest run``
        aufruft, will einen Testlauf und keine stillschweigend erfundene
        Konfiguration.

        Raises:
            ConfigError: Nur, wenn eine vorhandene Datei fehlerhaft ist.
        """
        paths = resolve_paths(config_option)
        if paths.exists:
            return cls(paths=paths, config=load_config(paths.config_file))
        return cls(
            paths=paths,
            config=load_config(example_config_path()),
            configured=False,
        )

    @classmethod
    def for_config(cls, config: AppConfig, paths: AppPaths) -> AppContext:
        """Erzeugt einen Kontext aus bereits geladenen Bestandteilen (Tests)."""
        return cls(paths=paths, config=config)

    # -- Abgeleitete Pfade -------------------------------------------------

    @property
    def base_dir(self) -> Path:
        """Basis fuer relative Pfade aus ``storage.*``."""
        return self.paths.base_dir

    @property
    def data_dir(self) -> Path:
        """Ordner der Messdatenbank."""
        return self.config.resolve_dir(self.config.storage.data_dir, self.base_dir)

    @property
    def db_path(self) -> Path:
        """Pfad der SQLite-Datenbank.

        Alle Testlaeufe liegen bewusst in *einer* Datenbank - nur so lassen sich
        zwei Laeufe ohne Umwege gegenueberstellen.
        """
        return self.data_dir / "fbtest.sqlite"

    @property
    def report_dir(self) -> Path:
        """Ordner der erzeugten HTML-Berichte."""
        return self.config.resolve_dir(self.config.storage.report_dir, self.base_dir)

    @property
    def export_dir(self) -> Path:
        """Ordner der CSV-/JSON-Exporte."""
        return self.config.resolve_dir(self.config.storage.export_dir, self.base_dir)

    @property
    def log_dir(self) -> Path:
        """Ordner der rotierenden Protokolldateien."""
        return self.config.resolve_dir(self.config.storage.log_dir, self.base_dir)

    def ensure_directories(self) -> None:
        """Legt alle Arbeitsverzeichnisse an."""
        self.config.ensure_directories(self.base_dir)

    # -- Aenderung ---------------------------------------------------------

    def apply(self, config: AppConfig) -> None:
        """Uebernimmt eine bereits validierte Konfiguration.

        Args:
            config: Die neue Konfiguration.
        """
        self.config = config
        # Nach dem ersten Speichern ist die Einrichtung abgeschlossen.
        self.configured = self.paths.exists

    def reload(self) -> None:
        """Liest die Konfigurationsdatei neu ein.

        Raises:
            ConfigError: Wenn die Datei inzwischen fehlt oder ungueltig ist.
                Der Kontext bleibt in diesem Fall unveraendert - eine kaputte
                Datei darf einen laufenden Betrieb nicht lahmlegen.
        """
        self.config = load_config(self.paths.config_file)
