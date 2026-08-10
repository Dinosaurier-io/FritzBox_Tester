"""Sicherstellen, dass nur eine Instanz je Rechner laeuft.

Zwei parallele Testlaeufe auf demselben Geraet verfaelschen sich gegenseitig:
Sie teilen sich Netzwerkkarte und Bandbreite, sodass die Messwerte beider Laeufe
unbrauchbar werden. Das README hat das bisher nur *dokumentiert* - die
Desktop-Anwendung muss es erzwingen.

Umsetzung: Die laufende Instanz haelt eine TCP-Verbindung auf einem festen Port
der Loopback-Schnittstelle. Ein Betriebssystem vergibt denselben Port kein
zweites Mal - das ist der Sperrmechanismus. Eine reine Sperrdatei genuegt
nicht: Stuerzt das Programm ab, bleibt sie liegen, und die Anwendung liesse
sich nie wieder starten.

Daneben liegt eine kleine Datei mit Adresse und Sitzungs-Token der laufenden
Instanz. Ein zweiter Start liest sie und bittet die erste, ihr Fenster nach
vorn zu holen, statt selbst eines zu oeffnen.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Port der Sperre. Bewusst hoch und ungewoehnlich, damit er nicht mit einem
#: anderen Dienst kollidiert. Es wird nichts darueber uebertragen.
LOCK_PORT = 47653

#: Name der Datei mit den Angaben zur laufenden Instanz.
HANDOFF_NAME = "instance.json"


@dataclass(frozen=True, slots=True)
class RunningInstance:
    """Angaben zur bereits laufenden Instanz."""

    pid: int
    url: str
    token: str


class SingleInstance:
    """Belegt die Sperre oder meldet, wer sie haelt.

    Verwendung::

        lock = SingleInstance(paths.base_dir)
        if not lock.acquire():
            other = lock.read_handoff()
            ...  # bestehendes Fenster nach vorn holen
    """

    def __init__(self, state_dir: Path, port: int = LOCK_PORT) -> None:
        """Initialisiert die Sperre.

        Args:
            state_dir: Ordner fuer die Uebergabedatei.
            port: Port der Sperre. Nur fuer Tests abweichend.
        """
        self.state_dir = state_dir
        self.port = port
        self._socket: socket.socket | None = None

    # -- Sperre ------------------------------------------------------------

    def acquire(self) -> bool:
        """Versucht, die Sperre zu belegen.

        Returns:
            True, wenn diese Instanz die einzige ist.
        """
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Ausdruecklich ohne SO_REUSEADDR: Wir *wollen* hier scheitern,
            # wenn der Port belegt ist - das ist der ganze Zweck.
            candidate.bind(("127.0.0.1", self.port))
            candidate.listen(1)
        except OSError:
            candidate.close()
            return False
        self._socket = candidate
        return True

    def release(self) -> None:
        """Gibt die Sperre frei und raeumt die Uebergabedatei ab."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self.clear_handoff()

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    @property
    def held(self) -> bool:
        """True, wenn diese Instanz die Sperre haelt."""
        return self._socket is not None

    # -- Uebergabe ---------------------------------------------------------

    @property
    def handoff_file(self) -> Path:
        """Pfad der Uebergabedatei."""
        return self.state_dir / HANDOFF_NAME

    def write_handoff(self, url: str, token: str) -> None:
        """Hinterlegt Adresse und Token fuer einen spaeteren zweiten Start.

        Die Datei liegt im Benutzerverzeichnis und enthaelt das Sitzungs-Token.
        Wer dort lesen kann, ist bereits als dieser Benutzer angemeldet und
        koennte ohnehin alles - sie schuetzt also nicht vor dem Benutzer,
        sondern ermoeglicht dem zweiten Start die Verstaendigung mit dem ersten.
        """
        payload = {"pid": os.getpid(), "url": url, "token": token}
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.handoff_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.warning("Uebergabedatei nicht schreibbar: %s", exc)

    def read_handoff(self) -> RunningInstance | None:
        """Liest die Angaben der laufenden Instanz.

        Returns:
            Die Angaben oder ``None``, wenn die Datei fehlt oder unbrauchbar
            ist. Beides ist kein Fehler - dann wird eben kein Fenster geholt.
        """
        try:
            data = json.loads(self.handoff_file.read_text(encoding="utf-8"))
            return RunningInstance(
                pid=int(data["pid"]), url=str(data["url"]), token=str(data["token"])
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.debug("Uebergabedatei nicht lesbar: %s", exc)
            return None

    def clear_handoff(self) -> None:
        """Entfernt die Uebergabedatei."""
        with contextlib.suppress(OSError):
            self.handoff_file.unlink(missing_ok=True)
