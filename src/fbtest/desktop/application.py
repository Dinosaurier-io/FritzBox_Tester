"""Zusammenschaltung der Desktop-Anwendung.

Drei Ablaeufe laufen gleichzeitig und muessen sauber zusammenspielen:

* **Webserver** (uvicorn) in einem Hintergrund-Thread. Dort laeuft auch der
  Testlauf - genau wie bisher im Dashboard.
* **Infobereich-Symbol** (pystray) in einem eigenen Thread.
* **Fenster** (pywebview) im Haupt-Thread. Das ist keine Wahl, sondern
  Vorgabe der Fenstersysteme.

Die wichtigste Regel steht in :meth:`DesktopApplication._on_window_close`:
**Das Schliessen des Fensters darf einen laufenden Testlauf niemals abbrechen.**
Der Server laeuft weiter, das Fenster verschwindet nur. Beendet wird
ausschliesslich ueber den ausdruecklichen Weg - und auch dort erst nach
Rueckfrage, wenn gerade gemessen wird.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from pathlib import Path

import uvicorn

from fbtest.context import AppContext
from fbtest.dashboard.app import TOKEN_HEADER, create_app
from fbtest.dashboard.controller import ControllerState, LastResult
from fbtest.desktop.icons import IconState, write_ico
from fbtest.desktop.notify import select_notifier
from fbtest.desktop.single_instance import SingleInstance
from fbtest.desktop.tray import TrayIcon, tray_available
from fbtest.desktop.watcher import RunWatcher
from fbtest.desktop.window import BrowserWindow, NativeWindow, native_window_available
from fbtest.runner import TestRunner

log = logging.getLogger(__name__)

#: Takt, in dem der Beobachter nach laufenden Ausfaellen sieht.
_TICK_SECONDS = 2.0


def free_port() -> int:
    """Sucht einen freien Port auf der Loopback-Schnittstelle.

    Ein fester Port waere bequemer, koennte aber belegt sein - und ein
    Programm, das dann gar nicht startet, ist schlechter als eines mit
    wechselnder Adresse. Die Adresse sieht ohnehin niemand: Sie steht im
    Fenster, nicht in der Adressleiste.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class DesktopApplication:
    """Die Desktop-Anwendung."""

    def __init__(self, context: AppContext, *, port: int | None = None) -> None:
        self.context = context
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"

        self.app = create_app(context)
        self.controller = self.app.state.controller
        self.token: str = self.app.state.token

        self.lock = SingleInstance(context.base_dir)
        self.watcher = RunWatcher(
            enabled=context.config.desktop.notifications,
            notify_outage_after_s=context.config.desktop.notify_outage_after_s,
        )

        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._tick_thread: threading.Thread | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._quitting = threading.Event()
        self.tray: TrayIcon | None = None
        self.window: NativeWindow | BrowserWindow | None = None

    # -- Ablauf ------------------------------------------------------------

    def run(self) -> int:
        """Startet die Anwendung und kehrt beim Beenden zurueck.

        Returns:
            Rueckgabewert fuer die Kommandozeile.
        """
        if not self.lock.acquire():
            return self._hand_over_to_running_instance()

        self._start_server()
        self.lock.write_handoff(self.url, self.token)
        self._wire_controller()
        self._start_tray()
        self._start_ticker()
        self._open_window()

        # Ab hier ist das Fenster geschlossen worden.
        self._shutdown()
        return 0

    def _hand_over_to_running_instance(self) -> int:
        """Bittet die laufende Instanz, ihr Fenster zu zeigen."""
        other = self.lock.read_handoff()
        if other is None:
            log.error("Es laeuft bereits eine Instanz, sie ist aber nicht erreichbar.")
            return 1

        try:
            import httpx

            httpx.post(
                f"{other.url}/api/window/show",
                headers={TOKEN_HEADER: other.token},
                timeout=5.0,
            )
            log.info("Bestehendes Fenster wurde nach vorn geholt (PID %s).", other.pid)
        except Exception as exc:
            log.warning("Bestehende Instanz nicht erreichbar (%s) - Browser wird geoeffnet.", exc)
            BrowserWindow(other.url).run()
        return 0

    # -- Bestandteile ------------------------------------------------------

    def _start_server(self) -> None:
        """Startet uvicorn im Hintergrund und wartet auf Bereitschaft."""
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(
            target=self._server.run, name="webserver", daemon=True
        )
        self._server_thread.start()

        for _ in range(100):
            if self._server.started:
                return
            time.sleep(0.05)
        log.warning("Der Webserver meldet nach 5 s keine Bereitschaft - wird trotzdem versucht.")

    def _wire_controller(self) -> None:
        """Verbindet Testlauf-Ereignisse mit Symbol und Meldungen."""
        self.app.state.on_show = self.show_window

        def started(runner: TestRunner) -> None:
            run_id = runner.run.id if runner.run else 0
            self.watcher.run_started(run_id, runner.name)
            self._refresh_tray()
            # Der Rueckruf laeuft bereits in der Ereignisschleife des Servers -
            # hier laesst sich die Aufgabe direkt anlegen.
            # Referenz halten: Ohne sie kann der Sammler die Aufgabe abraeumen.
            self._event_task = asyncio.create_task(
                self._consume_events(runner), name="desktop-events"
            )

        def finished(result: LastResult) -> None:
            self.watcher.run_finished(result.run_id, result.rows_written, result.interrupted)
            self._refresh_tray()

        self.controller.on_run_started(started)
        self.controller.on_run_finished(finished)

    async def _consume_events(self, runner: TestRunner) -> None:
        """Hoert die Ereignisse des laufenden Testlaufs mit.

        Bewusst ein eigenes Abonnement und nicht der Weg ueber die Datenbank:
        Ein Router-Neustart soll in dem Moment gemeldet werden, in dem er
        erkannt wird - nicht erst, wenn der Writer das naechste Mal schreibt.
        """
        subscription = runner.bus.subscribe()
        try:
            async for event in subscription:
                self.watcher.handle(event)
                self._refresh_tray()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ereignisbeobachtung der Desktop-Schale abgebrochen.")
        finally:
            subscription.close()

    def _start_tray(self) -> None:
        """Erzeugt das Symbol im Infobereich."""
        if not tray_available():
            log.info("Kein Infobereich verfuegbar - das Programm laeuft ohne Symbol.")
            return

        self.tray = TrayIcon(
            on_open=self.show_window,
            on_stop=self.stop_run,
            on_quit=self.quit,
            is_running=lambda: self.controller.state is not ControllerState.IDLE,
        )
        self.tray.run_detached()
        self.watcher.notifier = select_notifier(self.tray.icon)

    def _start_ticker(self) -> None:
        """Startet den Takt, der laufende Ausfaelle im Blick behaelt."""

        def loop() -> None:
            while not self._quitting.wait(_TICK_SECONDS):
                self.watcher.tick(time.time())
                self._refresh_tray()

        self._tick_thread = threading.Thread(target=loop, name="tray-tick", daemon=True)
        self._tick_thread.start()

    def _open_window(self) -> None:
        """Oeffnet das Fenster und blockiert, bis es geschlossen wird."""
        icon_path = self.context.base_dir / "fbtest.ico"
        try:
            write_ico(icon_path)
        except Exception as exc:
            log.debug("Fenstersymbol nicht erzeugbar: %s", exc)
            icon_path = None  # type: ignore[assignment]

        desktop = self.context.config.desktop
        if native_window_available():
            self.window = NativeWindow(
                url=self.url,
                title="FRITZ!Box-Langzeittest",
                width=desktop.window_width,
                height=desktop.window_height,
                icon=icon_path,
                on_close=self._on_window_close,
            )
            log.info("Fenster: nativ")
            self.window.run()
            return

        # Ohne natives Fenster gibt es nichts, das den Haupt-Thread haelt -
        # der Browser laeuft ja in einem eigenen Prozess.
        self.window = BrowserWindow(self.url, profile_dir=self.context.base_dir / "browser")
        log.info("Fenster: %s", self.window.kind)
        self.window.run()
        self._quitting.wait()

    # -- Aktionen ----------------------------------------------------------

    def _on_window_close(self) -> bool:
        """Entscheidet, was das Schliessen des Fensters bedeutet.

        Returns:
            True, wenn wirklich geschlossen werden darf.

        Bei einem laufenden Testlauf lautet die Antwort **immer** ``False``:
        Ein versehentlicher Klick auf das Kreuz darf keine Messung von 72
        Stunden vernichten. Das Programm wandert stattdessen in den
        Infobereich. Beendet wird ueber das Kontextmenue des Symbols.
        """
        if self._quitting.is_set():
            return True
        if self.controller.state is not ControllerState.IDLE:
            self.watcher.notifier.notify(
                "Testlauf laeuft weiter",
                "Das Fenster wurde geschlossen, die Messung laeuft im Hintergrund weiter. "
                "Beenden ueber das Symbol im Infobereich.",
            )
            return False
        return not self.context.config.desktop.minimize_to_tray

    def show_window(self) -> None:
        """Holt das Fenster nach vorn."""
        if self.window is not None:
            self.window.show()

    def stop_run(self) -> None:
        """Beendet einen laufenden Testlauf geordnet - wie Strg+C."""
        import httpx

        try:
            httpx.post(
                f"{self.url}/api/run/stop", headers={TOKEN_HEADER: self.token}, timeout=60.0
            )
        except Exception as exc:
            log.warning("Testlauf liess sich nicht beenden: %s", exc)

    def quit(self) -> None:
        """Beendet das Programm - der ausdrueckliche Weg."""
        if self._quitting.is_set():
            return
        self._quitting.set()
        if self.window is not None:
            self.window.destroy()

    # -- Aufraeumen --------------------------------------------------------

    def _refresh_tray(self) -> None:
        """Faerbt das Symbol nach dem aktuellen Zustand."""
        if self.tray is not None:
            self.tray.set_state(self.watcher.state)

    def _shutdown(self) -> None:
        """Faehrt alles geordnet herunter.

        Ein noch laufender Testlauf wird hier - und nur hier - beendet: Der
        Benutzer hat das Programm ausdruecklich verlassen.
        """
        self._quitting.set()
        if self.tray is not None:
            self.tray.set_state(IconState.IDLE)
            self.tray.stop()

        if self._server is not None:
            log.info("Webserver wird beendet ...")
            self._server.should_exit = True
            if self._server_thread is not None:
                # Grosszuegig bemessen: Der Lifespan schreibt beim Herunterfahren
                # noch den gesamten Puffer des Testlaufs in die Datenbank.
                self._server_thread.join(timeout=120)

        self.lock.release()
        log.info("Beendet.")


def build_icons(target: Path) -> list[Path]:
    """Schreibt die Symboldateien fuer das Programmpaket.

    Args:
        target: Zielordner.

    Returns:
        Die geschriebenen Dateien.
    """
    from fbtest.desktop.icons import write_png

    target.mkdir(parents=True, exist_ok=True)
    written = [write_ico(target / "fbtest.ico")]
    for size in (16, 32, 48, 64, 128, 256, 512):
        written.append(write_png(target / f"fbtest-{size}.png", size))
    for state in IconState:
        written.append(write_png(target / f"fbtest-{state}.png", 128, state))
    return written
