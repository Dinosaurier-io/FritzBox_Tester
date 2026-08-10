"""Gemeinsame Fixtures.

Alle Tests laufen bewusst **ohne echtes Netzwerk und ohne echte FRITZ!Box**:
Die Auswertelogik ist so geschnitten, dass sie mit synthetischen Daten gefuettert
werden kann. Nur so sind die Tests reproduzierbar und auch auf einem
Pruefungsrechner ohne passende Hardware lauffaehig.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fbtest.config import AppConfig
from fbtest.storage.database import Database

MINIMAL_CONFIG: dict[str, object] = {
    "ping": {
        "targets": [
            {"name": "fritzbox", "host": "192.168.178.1", "scope": "gateway"},
            {"name": "cloudflare", "host": "1.1.1.1", "scope": "internet"},
        ]
    }
}


@pytest.fixture
def config() -> AppConfig:
    """Eine minimale, gueltige Konfiguration."""
    return AppConfig.model_validate(MINIMAL_CONFIG)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """Eine frische Datenbank in einem temporaeren Ordner."""
    database = Database(tmp_path / "test.sqlite")
    yield database
    database.close()
