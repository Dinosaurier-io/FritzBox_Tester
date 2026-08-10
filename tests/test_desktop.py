"""Tests der Desktop-Schale.

Fenster und Infobereich lassen sich ohne grafische Sitzung nicht pruefen - ob
ein Symbol tatsaechlich erscheint, zeigt erst der Griff zur Maus. Genau deshalb
liegt die gesamte *Entscheidungslogik* ausserhalb dieser beiden Module:

* Wann eine Meldung faellig ist und welchen Zustand das Symbol zeigt,
  entscheidet der :class:`RunWatcher` - vollstaendig testbar.
* Ob eine zweite Instanz starten darf, entscheidet :class:`SingleInstance` -
  ebenfalls vollstaendig testbar.

Was hier gruen ist, kann im Fenster noch falsch aussehen. Was hier rot waere,
waere im Fenster garantiert kaputt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fbtest.core.models import Event, EventType, Severity
from fbtest.desktop.icons import IconState, draw_icon, write_ico, write_png
from fbtest.desktop.notify import NullNotifier, select_notifier
from fbtest.desktop.single_instance import SingleInstance
from fbtest.desktop.watcher import Notification, RunWatcher
from fbtest.desktop.window import find_app_mode_browser, native_window_available


class RecordingNotifier:
    """Merkt sich alle Meldungen, statt sie anzuzeigen."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    def notify(self, title: str, message: str, urgent: bool = False) -> bool:
        self.sent.append((title, message, urgent))
        return True


def make_event(kind: EventType, timestamp: float = 1000.0, message: str = "x") -> Event:
    """Baut ein Ereignis mit festem Zeitstempel."""
    return Event(kind, message, Severity.INFO, timestamp=timestamp)


# --------------------------------------------------------------------------
# Einzelinstanz
# --------------------------------------------------------------------------


class TestSingleInstance:
    """Nur eine Instanz je Rechner.

    Zwei parallele Testlaeufe auf demselben Geraet teilen sich Netzwerkkarte
    und Bandbreite - beide Messreihen waeren unbrauchbar.
    """

    def test_first_instance_gets_the_lock(self, tmp_path: Path) -> None:
        with SingleInstance(tmp_path, port=47999) as lock:
            assert lock.held

    def test_second_instance_is_refused(self, tmp_path: Path) -> None:
        with SingleInstance(tmp_path, port=47998):
            second = SingleInstance(tmp_path, port=47998)
            assert second.acquire() is False
            assert second.held is False

    def test_lock_is_released(self, tmp_path: Path) -> None:
        """Nach dem Beenden muss ein Neustart moeglich sein."""
        first = SingleInstance(tmp_path, port=47997)
        assert first.acquire()
        first.release()

        second = SingleInstance(tmp_path, port=47997)
        assert second.acquire() is True
        second.release()

    def test_handoff_round_trip(self, tmp_path: Path) -> None:
        lock = SingleInstance(tmp_path, port=47996)
        lock.write_handoff("http://127.0.0.1:1234", "geheim")

        other = SingleInstance(tmp_path, port=47996).read_handoff()
        assert other is not None
        assert other.url == "http://127.0.0.1:1234"
        assert other.token == "geheim"
        assert other.pid > 0

    def test_missing_handoff_is_not_an_error(self, tmp_path: Path) -> None:
        assert SingleInstance(tmp_path).read_handoff() is None

    def test_broken_handoff_is_not_an_error(self, tmp_path: Path) -> None:
        """Eine halb geschriebene Datei darf den Start nicht verhindern."""
        (tmp_path / "instance.json").write_text("{kaputt", encoding="utf-8")
        assert SingleInstance(tmp_path).read_handoff() is None

    def test_release_clears_the_handoff(self, tmp_path: Path) -> None:
        """Eine liegengebliebene Datei wuerde auf eine tote Instanz zeigen."""
        lock = SingleInstance(tmp_path, port=47995)
        lock.acquire()
        lock.write_handoff("http://127.0.0.1:1", "x")
        lock.release()
        assert not lock.handoff_file.exists()


# --------------------------------------------------------------------------
# Beobachter
# --------------------------------------------------------------------------


class TestWatcherState:
    """Zustand fuer das Symbol im Infobereich."""

    def test_idle_by_default(self) -> None:
        assert RunWatcher().state is IconState.IDLE

    def test_running(self) -> None:
        watcher = RunWatcher()
        watcher.run_started(1, "Test")
        assert watcher.state is IconState.RUNNING

    def test_outage_takes_precedence(self) -> None:
        watcher = RunWatcher()
        watcher.run_started(1, "Test")
        watcher.handle(make_event(EventType.OUTAGE_START))
        assert watcher.state is IconState.OUTAGE

    def test_back_to_running_after_outage(self) -> None:
        watcher = RunWatcher()
        watcher.run_started(1, "Test")
        watcher.handle(make_event(EventType.OUTAGE_START))
        watcher.handle(make_event(EventType.OUTAGE_END))
        assert watcher.state is IconState.RUNNING

    def test_idle_after_run(self) -> None:
        watcher = RunWatcher()
        watcher.run_started(1, "Test")
        watcher.run_finished(1, 100, interrupted=False)
        assert watcher.state is IconState.IDLE


class TestWatcherNotifications:
    """Wann gemeldet wird - und vor allem: wann nicht."""

    @pytest.fixture
    def watcher(self) -> RunWatcher:
        return RunWatcher(notifier=RecordingNotifier(), notify_outage_after_s=60.0)

    def test_reboot_is_always_reported(self, watcher: RunWatcher) -> None:
        """Der Befund, auf den der ganze Test zielt."""
        result = watcher.handle(make_event(EventType.ROUTER_REBOOT, message="Neustart erkannt"))
        assert isinstance(result, Notification)
        assert result.urgent
        assert "neu gestartet" in result.title
        assert result.message == "Neustart erkannt"

    def test_short_outage_stays_silent(self, watcher: RunWatcher) -> None:
        """Ein Aussetzer von drei Sekunden ist ein Messwert, kein Weckruf."""
        watcher.handle(make_event(EventType.OUTAGE_START, timestamp=1000.0))
        assert watcher.tick(1003.0) is None
        assert watcher.handle(make_event(EventType.OUTAGE_END, timestamp=1003.0)) is None

    def test_long_outage_is_reported_while_it_lasts(self, watcher: RunWatcher) -> None:
        """Nicht erst am Ende - sonst erfaehrt man es Stunden zu spaet."""
        watcher.handle(make_event(EventType.OUTAGE_START, timestamp=1000.0))
        assert watcher.tick(1030.0) is None
        result = watcher.tick(1061.0)
        assert isinstance(result, Notification)
        assert result.urgent

    def test_outage_is_reported_only_once(self, watcher: RunWatcher) -> None:
        """Eine Meldung im Zweisekundentakt wuerde weggeklickt."""
        watcher.handle(make_event(EventType.OUTAGE_START, timestamp=1000.0))
        watcher.tick(1100.0)
        assert watcher.tick(1200.0) is None
        assert watcher.tick(1300.0) is None

    def test_recovery_is_reported_after_a_reported_outage(self, watcher: RunWatcher) -> None:
        watcher.handle(make_event(EventType.OUTAGE_START, timestamp=1000.0))
        watcher.tick(1100.0)
        assert isinstance(watcher.handle(make_event(EventType.OUTAGE_END)), Notification)

    def test_second_outage_is_reported_again(self, watcher: RunWatcher) -> None:
        """Der Zaehler muss sich zuruecksetzen, sonst bleibt es bei einer Meldung."""
        watcher.handle(make_event(EventType.OUTAGE_START, timestamp=1000.0))
        watcher.tick(1100.0)
        watcher.handle(make_event(EventType.OUTAGE_END, timestamp=1100.0))

        watcher.handle(make_event(EventType.OUTAGE_START, timestamp=2000.0))
        assert watcher.tick(2100.0) is not None

    def test_finished_run_is_reported(self, watcher: RunWatcher) -> None:
        result = watcher.run_finished(7, 12345, interrupted=False)
        assert isinstance(result, Notification)
        assert "#7" in result.message

    def test_disabled_stays_completely_silent(self) -> None:
        recorder = RecordingNotifier()
        watcher = RunWatcher(notifier=recorder, enabled=False)
        watcher.handle(make_event(EventType.ROUTER_REBOOT))
        watcher.run_finished(1, 0, interrupted=True)
        assert recorder.sent == []

    def test_tick_without_outage_does_nothing(self, watcher: RunWatcher) -> None:
        assert watcher.tick(9999.0) is None


# --------------------------------------------------------------------------
# Symbole
# --------------------------------------------------------------------------


class TestIcons:
    """Selbst gezeichnete Symbole - keine fremden Marken."""

    @pytest.mark.parametrize("state", list(IconState))
    def test_every_state_has_its_own_look(self, state: IconState) -> None:
        assert state.color
        assert state.title

    def test_states_are_visually_distinct(self) -> None:
        """Der Sinn des Symbols ist, den Zustand auf einen Blick zu zeigen."""
        colors = {state.color for state in IconState}
        assert len(colors) == len(IconState)

    @pytest.mark.parametrize("size", [16, 32, 64, 256])
    def test_draws_at_every_size(self, size: int) -> None:
        image = draw_icon(size)
        assert image.size == (size, size)
        assert image.mode == "RGBA"

    def test_writes_png(self, tmp_path: Path) -> None:
        path = write_png(tmp_path / "symbol.png", 128)
        assert path.stat().st_size > 200

    def test_writes_ico_with_all_sizes(self, tmp_path: Path) -> None:
        """Fehlt eine Groesse, skaliert Windows selbst - und es wird unscharf."""
        from PIL import Image

        path = write_ico(tmp_path / "symbol.ico")
        with Image.open(path) as image:
            assert (16, 16) in image.info["sizes"]
            assert (256, 256) in image.info["sizes"]


# --------------------------------------------------------------------------
# Umgebung
# --------------------------------------------------------------------------


class TestEnvironment:
    """Erkennung der verfuegbaren Bausteine."""

    def test_notifier_selection_always_returns_something(self) -> None:
        """Ohne Meldungsdienst wird geschwiegen, nicht abgestuerzt."""
        assert select_notifier(None) is not None

    def test_null_notifier_is_honest(self) -> None:
        assert NullNotifier().notify("Titel", "Text") is False

    def test_window_strategy_is_decidable(self) -> None:
        """Es muss immer eine Stufe geben - notfalls der normale Browser."""
        assert isinstance(native_window_available(), bool)
        assert find_app_mode_browser() is None or isinstance(find_app_mode_browser(), str)
