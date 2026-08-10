"""Fensterrahmen um die Weboberflaeche.

Drei Stufen, absteigend nach Qualitaet:

1. **Natives Fenster** ueber ``pywebview``. Unter Windows 11 steckt dahinter
   WebView2, das mit Edge ohnehin auf jedem System vorhanden ist.
2. **Browser im App-Modus** (``--app=``). Ein eigenes Fenster ohne Adressleiste
   und ohne Reiter - optisch nah am nativen Fenster.
3. **Normaler Browser**. Funktioniert immer.

Stufe 2 und 3 sind kein Notnagel, sondern die vorgesehene Loesung unter Linux:
``pywebview`` braucht dort systemweit installierte GTK- und WebKit-Bibliotheken,
die sich nicht verlaesslich mitliefern lassen. Ein Programmpaket, das auf einer
Distribution laeuft und auf der naechsten nicht startet, waere schlechter als
gar keines.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Bekannte Browser, die einen App-Modus beherrschen.
_APP_MODE_BROWSERS = (
    "msedge",
    "chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave",
)


def native_window_available() -> bool:
    """Prueft, ob ein natives Fenster moeglich ist."""
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return True


class NativeWindow:
    """Natives Fenster ueber ``pywebview``."""

    kind = "nativ"

    def __init__(
        self,
        url: str,
        title: str,
        width: int,
        height: int,
        icon: Path | None = None,
        on_close: Callable[[], bool] | None = None,
    ) -> None:
        """Initialisiert das Fenster.

        Args:
            url: Adresse der Oberflaeche.
            title: Fenstertitel.
            width: Breite in Pixeln.
            height: Hoehe in Pixeln.
            icon: Pfad zum Fenstersymbol.
            on_close: Wird beim Schliessen gefragt. Liefert es ``False``, wird
                das Fenster nur versteckt statt geschlossen - so beendet das
                Schliessen niemals einen laufenden Testlauf.
        """
        self.url = url
        self.title = title
        self.width = width
        self.height = height
        self.icon = icon
        self.on_close = on_close
        self._window: Any = None
        self._hidden = False

    def run(self) -> None:
        """Oeffnet das Fenster und blockiert bis zum Beenden.

        Muss im Haupt-Thread laufen - das verlangen die Fenstersysteme aller
        Plattformen.
        """
        import webview

        self._window = webview.create_window(
            self.title,
            self.url,
            width=self.width,
            height=self.height,
            min_size=(900, 640),
            background_color="#12151a",
        )
        if self.on_close is not None:
            self._window.events.closing += self._closing

        webview.start(icon=str(self.icon) if self.icon else None)

    def _closing(self) -> bool:
        """Wird von pywebview beim Schliessen gefragt."""
        assert self.on_close is not None
        allow = self.on_close()
        if not allow:
            self.hide()
        return allow

    def show(self) -> None:
        """Holt das Fenster nach vorn."""
        if self._window is None:
            return
        try:
            self._window.show()
            self._hidden = False
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            log.debug("Fenster liess sich nicht anzeigen: %s", exc)

    def hide(self) -> None:
        """Versteckt das Fenster, ohne es zu schliessen."""
        if self._window is None:
            return
        try:
            self._window.hide()
            self._hidden = True
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            log.debug("Fenster liess sich nicht verstecken: %s", exc)

    def destroy(self) -> None:
        """Schliesst das Fenster endgueltig und beendet die Fensterschleife."""
        if self._window is None:
            return
        try:
            self._window.destroy()
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            log.debug("Fenster liess sich nicht schliessen: %s", exc)


def find_app_mode_browser() -> str | None:
    """Sucht einen Browser mit App-Modus."""
    for name in _APP_MODE_BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    if platform.system() == "Windows":
        for candidate in (
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ):
            if candidate.exists():
                return str(candidate)
    return None


class BrowserWindow:
    """Rueckfallstufe: Browser im App-Modus oder normal."""

    def __init__(self, url: str, profile_dir: Path | None = None) -> None:
        self.url = url
        self.profile_dir = profile_dir
        self._browser = find_app_mode_browser()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def kind(self) -> str:
        """Welche Stufe tatsaechlich benutzt wird."""
        return "Browser im App-Modus" if self._browser else "Browser"

    def run(self) -> None:
        """Oeffnet das Fenster. Kehrt sofort zurueck."""
        if self._browser:
            command = [self._browser, f"--app={self.url}"]
            if self.profile_dir:
                command.append(f"--user-data-dir={self.profile_dir}")
            try:
                self._process = subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return
            except OSError as exc:
                log.warning("App-Modus nicht startbar: %s", exc)
        webbrowser.open(self.url)

    def show(self) -> None:
        """Oeffnet erneut - ein bestehendes Fenster kommt damit nach vorn."""
        self.run()

    def hide(self) -> None:
        """Nicht moeglich; der Browser gehoert dem Benutzer."""

    def destroy(self) -> None:
        """Beendet ein selbst gestartetes App-Fenster."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
