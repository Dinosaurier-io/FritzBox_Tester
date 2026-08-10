"""Tests der Konfigurationsvalidierung."""

from __future__ import annotations

from pathlib import Path

import pytest

from fbtest.config import (
    AppConfig,
    ConfigError,
    StreamingProfile,
    WebProfile,
    format_duration,
    load_config,
    parse_duration,
)
from fbtest.paths import example_config_path

from .conftest import MINIMAL_CONFIG


class TestParseDuration:
    """Dauerangaben mit und ohne Einheit."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("30s", 30.0),
            ("15m", 900.0),
            ("24h", 86400.0),
            ("3d", 259200.0),
            ("1,5h", 5400.0),
            ("  2h  ", 7200.0),
            ("12H", 43200.0),
        ],
    )
    def test_valid(self, text: str, expected: float) -> None:
        assert parse_duration(text) == expected

    def test_plain_number_is_seconds(self) -> None:
        assert parse_duration(90) == 90.0

    @pytest.mark.parametrize("text", ["", "abc", "10x", "h", "-"])
    def test_invalid_raises(self, text: str) -> None:
        with pytest.raises(ValueError, match="Ungueltige Dauerangabe"):
            parse_duration(text)


def test_format_duration() -> None:
    assert format_duration(0) == "00:00:00"
    assert format_duration(3661) == "01:01:01"
    assert format_duration(90061) == "1 d 01:01:01"
    assert format_duration(-5) == "00:00:00"


class TestAppConfig:
    """Validierung des Wurzelmodells."""

    def test_minimal_config_is_valid(self, config: AppConfig) -> None:
        assert config.router.host == "192.168.178.1"
        assert len(config.ping.targets) == 2
        assert config.ping.targets[0].scope == "gateway"

    def test_ping_requires_at_least_one_target(self) -> None:
        with pytest.raises(ValueError, match="Mindestens ein Ping-Ziel"):
            AppConfig.model_validate({"ping": {"targets": []}})

    def test_unknown_key_is_rejected(self) -> None:
        """Tippfehler in der YAML sollen sofort auffallen, nicht stillschweigend wirkungslos sein."""
        data = {"ping": MINIMAL_CONFIG["ping"], "rooter": {"host": "x"}}
        with pytest.raises(ValueError, match="rooter"):
            AppConfig.model_validate(data)

    def test_duration_accepts_text(self) -> None:
        data = {**MINIMAL_CONFIG, "run": {"duration_s": "12h"}}
        assert AppConfig.model_validate(data).run.duration_s == 43200.0

    def test_traffic_profiles_are_discriminated_by_type(self) -> None:
        data = {
            **MINIMAL_CONFIG,
            "traffic": {
                "profiles": [
                    {"name": "w", "type": "web", "urls": ["https://example.org"]},
                    {"name": "s", "type": "streaming", "url": "https://example.org/f"},
                ]
            },
        }
        profiles = AppConfig.model_validate(data).traffic.profiles
        assert isinstance(profiles[0], WebProfile)
        assert isinstance(profiles[1], StreamingProfile)

    def test_web_profile_needs_urls(self) -> None:
        data = {
            **MINIMAL_CONFIG,
            "traffic": {"profiles": [{"name": "w", "type": "web", "urls": []}]},
        }
        with pytest.raises(ValueError, match="mindestens eine URL"):
            AppConfig.model_validate(data)

    def test_band_switch_needs_two_profiles(self) -> None:
        data = {**MINIMAL_CONFIG, "wlan": {"band_switch_enabled": True, "profiles": []}}
        with pytest.raises(ValueError, match="mindestens zwei WLAN-Profile"):
            AppConfig.model_validate(data)


class TestPassword:
    """Aufloesung des Router-Passworts."""

    def test_env_wins_over_plaintext(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = AppConfig.model_validate(
            {**MINIMAL_CONFIG, "router": {"password": "aus-datei"}}
        )
        monkeypatch.setenv("FRITZ_PASSWORD", "aus-umgebung")
        assert config.router.resolve_password() == "aus-umgebung"

    def test_falls_back_to_plaintext(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FRITZ_PASSWORD", raising=False)
        config = AppConfig.model_validate(
            {**MINIMAL_CONFIG, "router": {"password": "aus-datei"}}
        )
        assert config.router.resolve_password() == "aus-datei"


def test_snapshot_masks_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Konfigurations-Snapshot landet in der Datenbank - ohne Geheimnisse."""
    config = AppConfig.model_validate({**MINIMAL_CONFIG, "router": {"password": "geheim"}})
    snapshot = config.snapshot_json()
    assert "geheim" not in snapshot
    assert '"password": "***"' in snapshot


class TestLoadConfig:
    """Laden aus einer Datei."""

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="nicht gefunden"):
            load_config(tmp_path / "fehlt.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("ping: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigError, match="YAML-Syntaxfehler"):
            load_config(path)

    def test_valid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            "ping:\n"
            "  targets:\n"
            "    - name: fritzbox\n"
            "      host: 192.168.178.1\n"
            "      scope: gateway\n",
            encoding="utf-8",
        )
        assert load_config(path).ping.targets[0].name == "fritzbox"


def test_example_config_is_valid() -> None:
    """Die ausgelieferte Beispielkonfiguration muss immer gueltig bleiben."""
    example = example_config_path()
    assert example.exists(), f"Vorlage fehlt im Paket: {example}"
    config = load_config(example)
    assert config.ping.targets
    assert config.run.duration_s == 86400.0
