"""Einrichtung des Loggings.

Zwei Ausgabewege: eine rotierende Logdatei (vollstaendig, fuer die spaetere
Fehlersuche) und die Konsole (knapp, damit die Live-Anzeige lesbar bleibt).

Die Rotation ist fuer Langzeitlaeufe zwingend - ohne sie waechst die Logdatei
ueber Tage unbegrenzt.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from rich.logging import RichHandler

_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    console: bool = True,
    filename: str = "fbtest.log",
) -> Path:
    """Konfiguriert das Logging der Anwendung.

    Args:
        log_dir: Zielordner fuer die Logdateien.
        level: Loglevel als Text (``DEBUG``/``INFO``/``WARNING``/``ERROR``).
        max_bytes: Maximale Groesse einer Logdatei vor der Rotation.
        backup_count: Anzahl aufzubewahrender aelterer Logdateien.
        console: Ob zusaetzlich auf die Konsole geloggt wird.
        filename: Name der Logdatei.

    Returns:
        Pfad der aktiven Logdatei.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / filename

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(getattr(logging, level, logging.INFO))
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    root.addHandler(file_handler)

    if console:
        console_handler = RichHandler(
            rich_tracebacks=True,
            show_path=False,
            omit_repeated_times=False,
            log_time_format="%H:%M:%S",
        )
        console_handler.setLevel(getattr(logging, level, logging.INFO))
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console_handler)

    # Fremdbibliotheken sind im Dauerbetrieb sonst extrem gespraechig.
    for noisy in ("httpx", "httpcore", "matplotlib", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug("Logging eingerichtet: %s (Level %s)", log_path, level)
    return log_path
