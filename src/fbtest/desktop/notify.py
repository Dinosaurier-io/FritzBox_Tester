"""Systemmeldungen.

Wer einen Testlauf ueber Nacht laufen laesst, hat das Fenster nicht im Blick.
Ein Router-Neustart um drei Uhr morgens ist aber genau das Ereignis, auf das
der ganze Test abzielt - er soll auffallen, ohne dass jemand daneben sitzt.

Bewusst ohne zusaetzliche Abhaengigkeit: Unter Windows uebernimmt das ohnehin
vorhandene Symbol im Infobereich die Anzeige, unter Linux das Standardwerkzeug
``notify-send``. Ein Paket wie ``plyer`` wuerde Unterstuetzung fuer Android und
iOS mitschleppen, die hier nichts zu suchen hat.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """Sendet eine Systemmeldung."""

    def notify(self, title: str, message: str, urgent: bool = False) -> bool:
        """Zeigt eine Meldung an. ``False``, wenn das nicht moeglich war."""
        ...


class TrayNotifier:
    """Meldungen ueber das Symbol im Infobereich (Windows)."""

    def __init__(self, icon: object) -> None:
        """Args:
        icon: Ein ``pystray.Icon``. Bewusst untypisiert, damit dieses Modul
            ohne grafische Sitzung importierbar bleibt.
        """
        self._icon = icon

    def notify(self, title: str, message: str, urgent: bool = False) -> bool:
        """Zeigt eine Sprechblase am Symbol."""
        try:
            self._icon.notify(message, title)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            log.debug("Meldung ueber den Infobereich fehlgeschlagen: %s", exc)
            return False
        return True


class NotifySendNotifier:
    """Meldungen ueber ``notify-send`` (Linux)."""

    def notify(self, title: str, message: str, urgent: bool = False) -> bool:
        """Ruft ``notify-send`` auf."""
        try:
            subprocess.run(
                [
                    "notify-send",
                    "--app-name=fbtest",
                    f"--urgency={'critical' if urgent else 'normal'}",
                    title,
                    message,
                ],
                check=False,
                timeout=5,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("notify-send nicht verfuegbar: %s", exc)
            return False
        return True


class NullNotifier:
    """Tut nichts - fuer Systeme ohne Meldungsdienst und fuer Tests."""

    def notify(self, title: str, message: str, urgent: bool = False) -> bool:
        """Meldet ehrlich, dass nichts angezeigt wurde."""
        log.info("Meldung (nicht angezeigt): %s - %s", title, message)
        return False


def select_notifier(tray_icon: object | None = None) -> Notifier:
    """Waehlt den passenden Weg fuer Systemmeldungen.

    Args:
        tray_icon: Vorhandenes Infobereich-Symbol, falls eines existiert.
    """
    if tray_icon is not None and platform.system() == "Windows":
        return TrayNotifier(tray_icon)
    if platform.system() == "Linux":
        return NotifySendNotifier()
    if tray_icon is not None:
        return TrayNotifier(tray_icon)
    return NullNotifier()
