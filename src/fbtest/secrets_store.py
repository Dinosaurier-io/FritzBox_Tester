"""Ablage des FRITZ!Box-Passworts im Schluesselspeicher des Betriebssystems.

Bisher gab es zwei Wege, das Passwort bereitzustellen: eine Umgebungsvariable
oder Klartext in der ``config.yaml``. Beide sind fuer eine Desktop-Anwendung
unbrauchbar - eine Umgebungsvariable ueberlebt das Schliessen des Fensters
nicht, und Klartext in einer Datei, die man weitergibt, ist ein Fehler, der
irgendwann auffliegt.

Der Schluesselspeicher des Betriebssystems loest beides: Windows Credential
Manager, GNOME Keyring bzw. KWallet unter Linux, Schluesselbund unter macOS.

**Rangfolge** beim Ermitteln des Passworts:

1. Umgebungsvariable - behaelt bewusst den Vorrang. Automatisierung und
   CI-Laeufe muessen ohne Schluesselspeicher auskommen, und wer die Variable
   ausdruecklich setzt, meint sie auch.
2. Schluesselspeicher - der normale Weg der Desktop-Anwendung.
3. Klartext in der ``config.yaml`` - weiterhin moeglich, aber der letzte Ausweg.

Der Schluesselspeicher ist nicht ueberall vorhanden: Auf einem Linux-Server
ohne Sitzung gibt es keinen. Alle Zugriffe sind deshalb so gebaut, dass sie
im Fehlerfall ein sauberes "nicht verfuegbar" liefern, statt das Programm zu
beenden.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

log = logging.getLogger(__name__)

#: Name, unter dem die Eintraege im Schluesselspeicher erscheinen.
SERVICE_NAME = "fbtest"


class PasswordSource(StrEnum):
    """Woher das verwendete Passwort stammt."""

    ENVIRONMENT = "environment"
    KEYRING = "keyring"
    CONFIG = "config"
    NONE = "none"

    def describe(self) -> str:
        """Kurze Erklaerung fuer die Oberflaeche."""
        return {
            PasswordSource.ENVIRONMENT: "Umgebungsvariable",
            PasswordSource.KEYRING: "Schluesselspeicher des Betriebssystems",
            PasswordSource.CONFIG: "Klartext in der config.yaml",
            PasswordSource.NONE: "kein Passwort hinterlegt",
        }[self]


@dataclass(frozen=True, slots=True)
class ResolvedPassword:
    """Das ermittelte Passwort samt Herkunft."""

    value: str
    source: PasswordSource

    def __bool__(self) -> bool:
        """True, wenn ueberhaupt ein Passwort vorliegt."""
        return bool(self.value)


# --------------------------------------------------------------------------
# Schluesselspeicher
# --------------------------------------------------------------------------


class SecretBackend(Protocol):
    """Minimale Sicht auf einen Schluesselspeicher.

    Bewusst als Protokoll: Die Tests setzen eine Attrappe ein und brauchen
    damit weder einen Schluesselbund noch eine grafische Sitzung.
    """

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretStore:
    """Zugriff auf den Schluesselspeicher - fehlertolerant gekapselt."""

    def __init__(self, backend: SecretBackend | None = None) -> None:
        """Initialisiert den Zugriff.

        Args:
            backend: Zu verwendender Speicher. ``None`` = das echte
                ``keyring``-Paket, sofern installiert und nutzbar.
        """
        self._backend = backend
        self._probed = backend is not None
        self._error = ""
        self._name = type(backend).__name__ if backend is not None else ""

    # -- Verfuegbarkeit ----------------------------------------------------

    @property
    def backend(self) -> SecretBackend | None:
        """Der nutzbare Speicher, oder ``None``."""
        if not self._probed:
            self._probed = True
            self._backend = self._detect()
        return self._backend

    @property
    def available(self) -> bool:
        """True, wenn Passwoerter gespeichert werden koennen."""
        return self.backend is not None

    @property
    def error(self) -> str:
        """Grund, warum kein Speicher nutzbar ist (leer, wenn alles gut)."""
        self.backend  # noqa: B018 - loest die Pruefung aus
        return self._error

    @property
    def name(self) -> str:
        """Bezeichnung des verwendeten Speichers, fuer die Diagnose.

        Zeigt den tatsaechlichen Speicher an (z.B. ``WinVaultKeyring``) und
        nicht das Paket ``keyring`` - fuer die Fehlersuche zaehlt, welcher
        Speicher wirklich benutzt wird.
        """
        self.backend  # noqa: B018 - loest die Pruefung aus
        return self._name or "keiner"

    def _detect(self) -> SecretBackend | None:
        """Ermittelt einen nutzbaren Schluesselspeicher."""
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring
        except ImportError as exc:
            self._error = f"Das Paket 'keyring' ist nicht installiert ({exc})."
            return None

        try:
            current = keyring.get_keyring()
        except Exception as exc:
            self._error = f"Kein Schluesselspeicher verfuegbar: {exc}"
            return None

        if isinstance(current, FailKeyring):
            # Diesen Platzhalter setzt keyring, wenn es nichts Passendes findet -
            # typisch auf einem Linux-Server ohne grafische Sitzung.
            self._error = (
                "Auf diesem System ist kein Schluesselspeicher eingerichtet. "
                "Das Passwort kann weiterhin ueber eine Umgebungsvariable "
                "bereitgestellt werden."
            )
            return None

        self._name = type(current).__name__
        return keyring

    # -- Zugriff -----------------------------------------------------------

    def get(self, account: str) -> str | None:
        """Liest ein Passwort. ``None``, wenn keines hinterlegt ist."""
        backend = self.backend
        if backend is None:
            return None
        try:
            return backend.get_password(SERVICE_NAME, account)
        except Exception as exc:
            log.warning("Schluesselspeicher nicht lesbar: %s", exc)
            return None

    def set(self, account: str, password: str) -> None:
        """Speichert ein Passwort.

        Raises:
            SecretStoreError: Wenn kein Speicher verfuegbar ist oder das
                Schreiben fehlschlaegt.
        """
        backend = self.backend
        if backend is None:
            raise SecretStoreError(self._error or "Kein Schluesselspeicher verfuegbar.")
        try:
            backend.set_password(SERVICE_NAME, account, password)
        except Exception as exc:
            raise SecretStoreError(f"Passwort konnte nicht gespeichert werden: {exc}") from exc

    def delete(self, account: str) -> bool:
        """Entfernt ein Passwort.

        Returns:
            True, wenn etwas entfernt wurde.
        """
        backend = self.backend
        if backend is None:
            return False
        try:
            backend.delete_password(SERVICE_NAME, account)
        except Exception as exc:
            log.debug("Nichts zu loeschen im Schluesselspeicher: %s", exc)
            return False
        return True


class SecretStoreError(Exception):
    """Der Schluesselspeicher liess sich nicht beschreiben."""


#: Prozessweit genutzter Speicher. Der Schluesselspeicher ist eine Ressource
#: des Betriebssystems - es gibt genau einen, und die Ermittlung kostet Zeit.
_default_store: SecretStore | None = None


def default_store() -> SecretStore:
    """Liefert den prozessweit genutzten Schluesselspeicher."""
    global _default_store
    if _default_store is None:
        _default_store = SecretStore()
    return _default_store


def set_default_store(store: SecretStore | None) -> None:
    """Ersetzt den prozessweiten Speicher.

    Gedacht fuer Tests: Sie duerfen auf keinen Fall im echten
    Anmeldeinformationsspeicher des Benutzers herumschreiben. ``None`` setzt
    auf die automatische Ermittlung zurueck.
    """
    global _default_store
    _default_store = store


# --------------------------------------------------------------------------
# Aufloesung
# --------------------------------------------------------------------------


def account_name(host: str, username: str) -> str:
    """Schluessel, unter dem ein Passwort abgelegt wird.

    Host und Benutzername gehen ein, damit sich mehrere FRITZ!Boxen bzw.
    mehrere Benutzer derselben Box nicht gegenseitig ueberschreiben.

    Args:
        host: IP oder Hostname der Box.
        username: Benutzername; leer, wenn die Box ohne arbeitet.

    Returns:
        Der Schluessel, z.B. ``fbtest@192.168.178.1``.
    """
    return f"{username or '(standard)'}@{host}"


def resolve_password(
    host: str,
    username: str,
    password_env: str,
    plaintext: str,
    store: SecretStore | None = None,
    environ: dict[str, str] | None = None,
) -> ResolvedPassword:
    """Ermittelt das zu verwendende Passwort nach fester Rangfolge.

    Args:
        host: IP oder Hostname der Box.
        username: Benutzername der Box.
        password_env: Name der Umgebungsvariablen.
        plaintext: Klartexteintrag aus der Konfiguration.
        store: Schluesselspeicher; ``None`` = der des Systems.
        environ: Umgebung; ``None`` = die echte. Nur fuer Tests.

    Returns:
        Passwort und Herkunft. Bei fehlendem Passwort ein leerer Wert mit
        :attr:`PasswordSource.NONE`.
    """
    env = os.environ if environ is None else environ

    from_env = env.get(password_env, "") if password_env else ""
    if from_env:
        return ResolvedPassword(from_env, PasswordSource.ENVIRONMENT)

    from_store = (store or default_store()).get(account_name(host, username))
    if from_store:
        return ResolvedPassword(from_store, PasswordSource.KEYRING)

    if plaintext:
        return ResolvedPassword(plaintext, PasswordSource.CONFIG)

    return ResolvedPassword("", PasswordSource.NONE)
