"""Tests der Standby-Unterdrueckung.

Die eigentliche Sperre ist plattformabhaengig und laesst sich in einem Test
nicht sinnvoll nachweisen - ob Windows tatsaechlich wach bleibt, zeigt erst ein
mehrstuendiger Lauf. Pruefbar und wichtig ist dagegen das Verhalten drumherum:
Ein Fehlschlag darf keinen Testlauf verhindern, und die Sperre muss zuverlaessig
wieder aufgehoben werden.
"""

from __future__ import annotations

import pytest

from fbtest.power import (
    PowerKeeper,
    UnsupportedPowerBackend,
    select_backend,
)


class FakeBackend:
    """Zaehlt Aufrufe und laesst sich zum Scheitern bringen."""

    description = "Attrappe"

    def __init__(self, *, works: bool = True, raises: bool = False) -> None:
        self.works = works
        self.raises = raises
        self.acquired = 0
        self.released = 0

    def acquire(self) -> bool:
        self.acquired += 1
        if self.raises:
            raise OSError("Schnittstelle nicht verfuegbar")
        return self.works

    def release(self) -> None:
        self.released += 1


class TestBackendSelection:
    """Auswahl der plattformabhaengigen Umsetzung."""

    def test_returns_something_usable(self) -> None:
        backend = select_backend()
        assert backend.description

    def test_unsupported_backend_is_honest(self) -> None:
        """Kein Vortaeuschen: Wo es nicht geht, wird das gemeldet."""
        backend = UnsupportedPowerBackend()
        assert backend.acquire() is False
        backend.release()


class TestPowerKeeper:
    """Verhalten der Sperre."""

    def test_acquires_and_releases(self) -> None:
        backend = FakeBackend()
        keeper = PowerKeeper(backend=backend)

        assert keeper.acquire() is True
        assert keeper.active
        keeper.release()
        assert not keeper.active
        assert (backend.acquired, backend.released) == (1, 1)

    def test_context_manager(self) -> None:
        backend = FakeBackend()
        with PowerKeeper(backend=backend) as keeper:
            assert keeper.active
        assert backend.released == 1

    def test_disabled_does_nothing(self) -> None:
        backend = FakeBackend()
        keeper = PowerKeeper(enabled=False, backend=backend)

        assert keeper.acquire() is False
        assert backend.acquired == 0
        assert "abgeschaltet" in keeper.status_text

    def test_failure_is_not_fatal(self) -> None:
        """Lieber ein Testlauf mit Standby-Risiko als gar keiner."""
        keeper = PowerKeeper(backend=FakeBackend(works=False))

        assert keeper.acquire() is False
        assert not keeper.active
        assert "NICHT unterdrueckt" in keeper.status_text

    def test_exception_is_caught(self) -> None:
        keeper = PowerKeeper(backend=FakeBackend(raises=True))

        assert keeper.acquire() is False
        assert "OSError" in keeper.error

    def test_release_without_acquire_is_harmless(self) -> None:
        backend = FakeBackend()
        PowerKeeper(backend=backend).release()
        assert backend.released == 0

    def test_double_acquire_is_idempotent(self) -> None:
        """Sonst haetten wir am Ende mehr Sperren als Freigaben."""
        backend = FakeBackend()
        keeper = PowerKeeper(backend=backend)
        keeper.acquire()
        keeper.acquire()
        assert backend.acquired == 1

    @pytest.mark.parametrize("works", [True, False])
    def test_status_text_is_always_meaningful(self, works: bool) -> None:
        keeper = PowerKeeper(backend=FakeBackend(works=works))
        keeper.acquire()
        assert "Energiesparmodus" in keeper.status_text
