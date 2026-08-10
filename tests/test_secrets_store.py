"""Tests der Passwortablage.

Laeuft ohne echten Schluesselspeicher: Der Zugriff ist als Protokoll
formuliert, sodass hier eine Attrappe eingesetzt werden kann. Das ist keine
Bequemlichkeit, sondern Voraussetzung - auf einem Pruefungsrechner oder in
einer CI-Umgebung gibt es haeufig gar keinen Schluesselspeicher.
"""

from __future__ import annotations

import pytest

from fbtest.config import AppConfig
from fbtest.secrets_store import (
    SERVICE_NAME,
    PasswordSource,
    SecretStore,
    SecretStoreError,
    account_name,
    resolve_password,
)


class FakeBackend:
    """Ein Schluesselspeicher im Arbeitsspeicher."""

    def __init__(self, *, broken: bool = False) -> None:
        self.entries: dict[tuple[str, str], str] = {}
        self.broken = broken

    def get_password(self, service: str, username: str) -> str | None:
        if self.broken:
            raise RuntimeError("Schluesselspeicher gesperrt")
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.broken:
            raise RuntimeError("Schluesselspeicher gesperrt")
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.broken:
            raise RuntimeError("Schluesselspeicher gesperrt")
        del self.entries[(service, username)]


@pytest.fixture
def store() -> SecretStore:
    """Ein Speicher mit Attrappe."""
    return SecretStore(FakeBackend())


class TestAccountName:
    """Schluessel, unter dem abgelegt wird."""

    def test_includes_user_and_host(self) -> None:
        assert account_name("192.168.178.1", "fbtest") == "fbtest@192.168.178.1"

    def test_marks_missing_username(self) -> None:
        """Manche Boxen arbeiten ohne Benutzernamen - das braucht einen Platzhalter."""
        assert account_name("192.168.178.1", "") == "(standard)@192.168.178.1"

    def test_two_boxes_do_not_collide(self) -> None:
        """Sonst ueberschriebe die zweite FRITZ!Box das Passwort der ersten."""
        assert account_name("10.0.0.1", "a") != account_name("10.0.0.2", "a")


class TestSecretStore:
    """Zugriff auf den Speicher."""

    def test_round_trip(self, store: SecretStore) -> None:
        store.set("konto", "geheim")
        assert store.get("konto") == "geheim"

    def test_missing_entry(self, store: SecretStore) -> None:
        assert store.get("gibtsnicht") is None

    def test_delete(self, store: SecretStore) -> None:
        store.set("konto", "geheim")
        assert store.delete("konto") is True
        assert store.get("konto") is None

    def test_delete_of_missing_entry_is_harmless(self, store: SecretStore) -> None:
        assert store.delete("gibtsnicht") is False

    def test_uses_the_documented_service_name(self, store: SecretStore) -> None:
        """Der Name erscheint im Anmeldeinformationsspeicher des Systems."""
        store.set("konto", "geheim")
        backend = store.backend
        assert isinstance(backend, FakeBackend)
        assert (SERVICE_NAME, "konto") in backend.entries

    def test_read_failure_is_not_fatal(self) -> None:
        """Ein gesperrter Speicher darf keinen Testlauf verhindern."""
        store = SecretStore(FakeBackend(broken=True))
        assert store.get("konto") is None

    def test_write_failure_is_reported(self) -> None:
        """Beim Speichern dagegen muss der Nutzer es erfahren."""
        store = SecretStore(FakeBackend(broken=True))
        with pytest.raises(SecretStoreError, match="nicht gespeichert"):
            store.set("konto", "geheim")


class TestResolution:
    """Rangfolge der drei Quellen."""

    def test_environment_wins(self, store: SecretStore) -> None:
        """Wer die Variable ausdruecklich setzt, meint sie auch."""
        store.set(account_name("box", "u"), "aus-speicher")
        result = resolve_password("box", "u", "FRITZ_PASSWORD", "aus-datei", store,
                                  {"FRITZ_PASSWORD": "aus-umgebung"})
        assert result.value == "aus-umgebung"
        assert result.source is PasswordSource.ENVIRONMENT

    def test_keyring_beats_plaintext(self, store: SecretStore) -> None:
        store.set(account_name("box", "u"), "aus-speicher")
        result = resolve_password("box", "u", "FRITZ_PASSWORD", "aus-datei", store, {})
        assert result.value == "aus-speicher"
        assert result.source is PasswordSource.KEYRING

    def test_plaintext_as_last_resort(self, store: SecretStore) -> None:
        result = resolve_password("box", "u", "FRITZ_PASSWORD", "aus-datei", store, {})
        assert result.value == "aus-datei"
        assert result.source is PasswordSource.CONFIG

    def test_nothing_available(self, store: SecretStore) -> None:
        result = resolve_password("box", "u", "FRITZ_PASSWORD", "", store, {})
        assert not result
        assert result.source is PasswordSource.NONE

    def test_empty_environment_variable_is_skipped(self, store: SecretStore) -> None:
        """Eine leer gesetzte Variable darf das Passwort nicht verdecken."""
        result = resolve_password("box", "u", "FRITZ_PASSWORD", "aus-datei", store,
                                  {"FRITZ_PASSWORD": ""})
        assert result.source is PasswordSource.CONFIG

    @pytest.mark.parametrize("source", list(PasswordSource))
    def test_every_source_is_explainable(self, source: PasswordSource) -> None:
        assert source.describe()


class TestConfigIntegration:
    """Anbindung an das Konfigurationsmodell."""

    def test_config_uses_the_resolution(self, config: AppConfig, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("FRITZ_PASSWORD", "aus-umgebung")
        resolved = config.router.resolve_password_source()
        assert resolved.value == "aus-umgebung"
        assert resolved.source is PasswordSource.ENVIRONMENT

    def test_snapshot_still_masks_the_password(self, config: AppConfig) -> None:
        """Der Schnappschuss landet in der Datenbank und in jedem Export."""
        config.router.password = "geheim"
        assert "geheim" not in config.snapshot_json()
        assert '"***"' in config.snapshot_json()
