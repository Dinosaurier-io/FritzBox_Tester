"""Tests des Laufzeitkontexts.

Der Kontext existiert, weil die Konfiguration zur Laufzeit austauschbar werden
muss. Genau das wird hier geprueft: dass abgeleitete Pfade einer Aenderung
folgen, statt einen alten Stand festzuhalten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fbtest.config import AppConfig, ConfigError
from fbtest.context import AppContext
from fbtest.paths import AppPaths, PathOrigin

from .conftest import MINIMAL_CONFIG


@pytest.fixture
def context(config: AppConfig, tmp_path: Path) -> AppContext:
    """Ein Kontext mit temporaeren Arbeitsverzeichnissen."""
    paths = AppPaths(tmp_path / "config.yaml", tmp_path, PathOrigin.EXPLICIT)
    return AppContext(paths=paths, config=config)


class TestDerivedPaths:
    """Alle Arbeitspfade leiten sich aus Konfiguration und Basis ab."""

    def test_default_layout(self, context: AppContext, tmp_path: Path) -> None:
        assert context.base_dir == tmp_path
        assert context.data_dir == tmp_path / "data"
        assert context.db_path == tmp_path / "data" / "fbtest.sqlite"
        assert context.report_dir == tmp_path / "reports"
        assert context.export_dir == tmp_path / "exports"
        assert context.log_dir == tmp_path / "logs"

    def test_absolute_paths_are_kept(self, context: AppContext, tmp_path: Path) -> None:
        """Ein absoluter Pfad in der Konfiguration wird nicht umgebogen."""
        elsewhere = tmp_path / "woanders"
        context.config.storage.data_dir = elsewhere
        assert context.db_path == elsewhere / "fbtest.sqlite"

    def test_ensure_directories_creates_all(self, context: AppContext) -> None:
        context.ensure_directories()
        for path in (context.data_dir, context.report_dir, context.export_dir, context.log_dir):
            assert path.is_dir()


class TestConfigExchange:
    """Der eigentliche Zweck: Konfiguration im Betrieb austauschen."""

    def test_apply_changes_derived_paths(self, context: AppContext, tmp_path: Path) -> None:
        """Nach dem Austausch muessen alle abgeleiteten Pfade mitziehen.

        Genau hier lag der Fehler des alten Entwurfs: Wer sich ``db_path``
        einmal gemerkt hatte, arbeitete danach auf der falschen Datenbank.
        """
        before = context.db_path
        changed = AppConfig.model_validate({**MINIMAL_CONFIG, "storage": {"data_dir": "messwerte"}})
        context.apply(changed)

        assert context.db_path != before
        assert context.db_path == tmp_path / "messwerte" / "fbtest.sqlite"

    def test_reload_reads_from_disk(self, context: AppContext) -> None:
        context.paths.config_file.write_text(
            "ping:\n"
            "  interval_s: 2.5\n"
            "  targets:\n"
            "    - name: fritzbox\n"
            "      host: 192.168.178.1\n"
            "      scope: gateway\n",
            encoding="utf-8",
        )
        context.reload()
        assert context.config.ping.interval_s == 2.5

    def test_broken_file_leaves_context_untouched(self, context: AppContext) -> None:
        """Eine kaputte Datei darf den laufenden Betrieb nicht lahmlegen."""
        before = context.config
        context.paths.config_file.write_text("ping: [das ist kein objekt", encoding="utf-8")

        with pytest.raises(ConfigError):
            context.reload()
        assert context.config is before


class TestLoading:
    """Erzeugung ueber den regulaeren Weg."""

    def test_load_finds_config_in_working_dir(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "config.yaml").write_text(
            "ping:\n"
            "  targets:\n"
            "    - name: fritzbox\n"
            "      host: 192.168.178.1\n"
            "      scope: gateway\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FBTEST_HOME", raising=False)

        context = AppContext.load()
        assert context.config.ping.targets[0].name == "fritzbox"
        assert context.base_dir == tmp_path.resolve()

    def test_load_reports_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="nicht gefunden"):
            AppContext.load(tmp_path / "fehlt.yaml")
