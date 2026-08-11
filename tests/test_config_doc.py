"""Tests der Einstellungs-Referenz (``docs/EINSTELLUNGEN.md``).

Eine Referenz veraltet lautlos: Ein neues Feld erscheint einfach nicht, und
niemand vermisst es. Diese Tests machen daraus einen roten Test statt einer
Luecke, die erst der Leser bemerkt.
"""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from fbtest.config import AppConfig
from fbtest.config_doc import (
    DEFAULT_TARGET,
    SECTION_INTROS,
    SECTION_TITLES,
    render,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / DEFAULT_TARGET

NEU_ERZEUGEN = ".\\.venv\\Scripts\\python.exe -m fbtest.config_doc"


def alle_felder(
    model: type[pydantic.BaseModel], prefix: str = ""
) -> list[tuple[str, pydantic.fields.FieldInfo]]:
    """Sammelt alle Felder eines Modells samt Untermodellen."""
    gefunden: list[tuple[str, pydantic.fields.FieldInfo]] = []
    for name, field in model.model_fields.items():
        pfad = f"{prefix}{name}"
        gefunden.append((pfad, field))
        annotation = field.annotation
        kandidaten = getattr(annotation, "__args__", None) or [annotation]
        for kandidat in kandidaten:
            if isinstance(kandidat, type) and issubclass(kandidat, pydantic.BaseModel):
                gefunden.extend(alle_felder(kandidat, f"{pfad}."))
    return gefunden


class TestReferenz:
    """Die Datei im Projekt muss dem Stand der Modelle entsprechen."""

    def test_datei_ist_aktuell(self) -> None:
        """Sonst dokumentiert die Referenz einen Stand, den es nicht mehr gibt."""
        assert DOC_PATH.exists(), f"{DOC_PATH} fehlt - erzeugen mit: {NEU_ERZEUGEN}"
        assert DOC_PATH.read_text(encoding="utf-8") == render(), (
            f"{DOC_PATH.name} passt nicht mehr zu den Modellen. Neu erzeugen mit: {NEU_ERZEUGEN}"
        )

    def test_jeder_abschnitt_hat_titel_und_einleitung(self) -> None:
        """Eine Tabelle ohne Einleitung sagt nicht, wozu der Abschnitt da ist."""
        for name in AppConfig.model_fields:
            assert name in SECTION_TITLES, f"Abschnitt '{name}' hat keine Ueberschrift"
            assert SECTION_INTROS.get(name), f"Abschnitt '{name}' hat keine Einleitung"

    def test_keine_ueberzaehligen_eintraege(self) -> None:
        """Ein Text zu einem entfernten Abschnitt wuerde nie auffallen."""
        for tabelle, bezeichnung in ((SECTION_TITLES, "Titel"), (SECTION_INTROS, "Einleitung")):
            uebrig = set(tabelle) - set(AppConfig.model_fields)
            assert not uebrig, f"{bezeichnung} ohne zugehoerigen Abschnitt: {sorted(uebrig)}"


class TestFelder:
    """Jedes Feld muss sich selbst erklaeren."""

    @pytest.mark.parametrize("pfad,field", alle_felder(AppConfig))
    def test_feld_hat_beschreibung(self, pfad: str, field: pydantic.fields.FieldInfo) -> None:
        """Die Beschreibung erscheint in der Referenz *und* als Hinweis im Formular.

        Fehlt sie, steht das Feld in beiden ohne Erklaerung da - und die
        Einstellung ist praktisch nur fuer den auffindbar, der den Quelltext
        kennt. Abschnitte sind ausgenommen, ihr Text steht in SECTION_INTROS.
        """
        if pfad in AppConfig.model_fields:
            return
        assert field.description, f"Feld '{pfad}' hat keine description"

    def test_jedes_feld_steht_in_der_referenz(self) -> None:
        """Die Vollstaendigkeit ist der einzige Zweck des Dokuments."""
        text = render()
        fehlend = [
            pfad
            for pfad, _ in alle_felder(AppConfig)
            if pfad not in AppConfig.model_fields and f"| `{pfad.split('.')[-1]}` |" not in text
        ]
        assert not fehlend, f"Nicht in der Referenz: {fehlend}"
