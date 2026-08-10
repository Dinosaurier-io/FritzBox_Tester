"""Supervisor fuer alle Modul-Tasks.

Zentrale Zusicherung des Systems: **Ein einzelnes Modul darf den Testlauf nie
beenden.** Stuerzt ein Modul ab, wird das protokolliert, als Ereignis gespeichert
und das Modul mit exponentiell wachsender Wartezeit (1 s -> max. 60 s) neu
gestartet.

Zusaetzlich laeuft hier die Standby-Erkennung: Weicht die Wall-Clock deutlich
staerker fort als die monotone Uhr, war der Rechner vermutlich im Ruhezustand.
Diese Luecke wird als ``SYSTEM_GAP`` markiert und ausdruecklich *nicht* als
Router-Ausfall gewertet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fbtest.core.events import EventBus
from fbtest.core.models import EventType, Severity

log = logging.getLogger(__name__)

#: Eine Modul-Fabrik erzeugt bei jedem (Neu-)Start eine frische Coroutine.
TaskFactory = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class TaskState:
    """Laufzeitzustand eines ueberwachten Tasks."""

    name: str
    factory: TaskFactory
    restarts: int = 0
    last_error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    running: bool = False


class Scheduler:
    """Startet, ueberwacht und restartet die Modul-Tasks eines Testlaufs."""

    def __init__(
        self,
        bus: EventBus,
        backoff_start_s: float = 1.0,
        backoff_max_s: float = 60.0,
        gap_threshold_s: float = 120.0,
    ) -> None:
        """Initialisiert den Supervisor.

        Args:
            bus: Event-Bus fuer Ereignismeldungen.
            backoff_start_s: Wartezeit vor dem ersten Neustartversuch.
            backoff_max_s: Obergrenze der Wartezeit.
            gap_threshold_s: Ab dieser Abweichung gilt eine Zeitluecke als Standby.
        """
        self._bus = bus
        self._backoff_start = backoff_start_s
        self._backoff_max = backoff_max_s
        self._gap_threshold = gap_threshold_s
        self._states: dict[str, TaskState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()

    # -- Registrierung -----------------------------------------------------

    def add(self, name: str, factory: TaskFactory) -> None:
        """Registriert ein Modul unter einem eindeutigen Namen.

        Args:
            name: Anzeigename, erscheint in Log und Ereignissen.
            factory: Erzeugt die auszufuehrende Coroutine. Wird bei jedem
                Neustart erneut aufgerufen.
        """
        if name in self._states:
            raise ValueError(f"Task '{name}' ist bereits registriert.")
        self._states[name] = TaskState(name=name, factory=factory)

    @property
    def task_names(self) -> list[str]:
        """Namen aller registrierten Tasks."""
        return list(self._states)

    def state(self, name: str) -> TaskState:
        """Liefert den Laufzeitzustand eines Tasks."""
        return self._states[name]

    @property
    def states(self) -> list[TaskState]:
        """Laufzeitzustand aller Tasks."""
        return list(self._states.values())

    # -- Ausfuehrung -------------------------------------------------------

    async def run(self, duration_s: float | None = None) -> None:
        """Startet alle Module und laeuft bis Ablauf der Dauer oder bis Abbruch.

        Args:
            duration_s: Gesamtlaufzeit in Sekunden; ``None`` = unbegrenzt.
        """
        for name in self._states:
            self._tasks[name] = asyncio.create_task(self._supervise(name), name=f"sv:{name}")

        watchdog = asyncio.create_task(self._clock_watchdog(), name="sv:clock")

        try:
            if duration_s is None:
                await self._stopping.wait()
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=duration_s)
        finally:
            self._stopping.set()
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            await self._stop_all()

    def request_stop(self) -> None:
        """Fordert das geordnete Beenden aller Tasks an."""
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        """True, sobald das Beenden angefordert wurde."""
        return self._stopping.is_set()

    async def _stop_all(self) -> None:
        """Bricht alle Supervisor-Tasks ab und wartet auf deren Ende."""
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def _supervise(self, name: str) -> None:
        """Fuehrt ein Modul aus und startet es nach Abstuerzen neu."""
        state = self._states[name]
        backoff = self._backoff_start

        while not self._stopping.is_set():
            state.running = True
            state.started_at = time.monotonic()
            self._bus.emit(
                EventType.MODULE_START,
                f"Modul '{name}' gestartet.",
                Severity.DEBUG,
                module=name,
            )
            try:
                await state.factory()
                # Regulaeres Ende (z.B. Modul hat seine Arbeit abgeschlossen).
                log.info("Modul '%s' regulaer beendet.", name)
                state.running = False
                return
            except asyncio.CancelledError:
                state.running = False
                self._bus.emit(
                    EventType.MODULE_STOP,
                    f"Modul '{name}' beendet.",
                    Severity.DEBUG,
                    module=name,
                )
                raise
            except Exception as exc:
                state.running = False
                state.restarts += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
                uptime = time.monotonic() - state.started_at
                log.exception("Modul '%s' abgestuerzt (lief %.1f s).", name, uptime)
                self._bus.emit(
                    EventType.MODULE_CRASH,
                    f"Modul '{name}' abgestuerzt: {state.last_error}",
                    Severity.ERROR,
                    module=name,
                    restarts=state.restarts,
                    uptime_s=round(uptime, 1),
                )
                # Lief das Modul lange stabil, ist der Backoff wieder zurueckzusetzen.
                if uptime > self._backoff_max:
                    backoff = self._backoff_start

            if self._stopping.is_set():
                return

            self._bus.emit(
                EventType.MODULE_RESTART,
                f"Modul '{name}' wird in {backoff:.0f} s neu gestartet "
                f"(Versuch {state.restarts}).",
                Severity.WARNING,
                module=name,
                backoff_s=backoff,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def _clock_watchdog(self, tick_s: float = 5.0) -> None:
        """Erkennt Systemschlaf/Zeitspruenge und meldet sie als ``SYSTEM_GAP``."""
        last_mono = time.monotonic()
        last_wall = time.time()

        while not self._stopping.is_set():
            await asyncio.sleep(tick_s)
            now_mono = time.monotonic()
            now_wall = time.time()
            mono_delta = now_mono - last_mono
            wall_delta = now_wall - last_wall

            # Standby: die Ereignisschleife stand still, beide Uhren springen.
            if mono_delta > self._gap_threshold:
                self._bus.emit(
                    EventType.SYSTEM_GAP,
                    f"Zeitluecke von {mono_delta:.0f} s erkannt (vermutlich Standby oder "
                    "Systemauslastung). Dieser Zeitraum wird nicht als Router-Ausfall gewertet.",
                    Severity.WARNING,
                    gap_s=round(mono_delta, 1),
                    kind="standby",
                )
            # Reine Wall-Clock-Verschiebung: Zeitumstellung oder NTP-Sprung.
            elif abs(wall_delta - mono_delta) > self._gap_threshold:
                self._bus.emit(
                    EventType.SYSTEM_GAP,
                    f"Systemzeit wurde um {wall_delta - mono_delta:+.0f} s verstellt "
                    "(Zeitumstellung oder Zeitsynchronisation). Dauern bleiben korrekt, "
                    "da intern die monotone Uhr verwendet wird.",
                    Severity.WARNING,
                    shift_s=round(wall_delta - mono_delta, 1),
                    kind="clock_shift",
                )

            last_mono, last_wall = now_mono, now_wall
