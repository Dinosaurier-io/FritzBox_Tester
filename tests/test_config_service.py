"""Tests des Konfigurationsdienstes.

Der Dienst schreibt in die Datei, die der Nutzer von Hand gepflegt hat. Ein
Fehler hier zerstoert Arbeit, die sich nicht wiederherstellen laesst - deshalb
sind die Zusicherungen einzeln geprueft:

* Kommentare bleiben erhalten.
* Ungueltige Eingaben schreiben ueberhaupt nichts.
* Das Passwort geht beim Speichern nicht verloren und verlaesst den Prozess nie.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fbtest.config import AppConfig, ConfigError, load_config
from fbtest.config_service import (
    PASSWORD_MASK,
    ConfigValidationError,
    json_schema,
    read_raw,
    save,
    save_raw,
    to_form_values,
    validate,
    validate_raw,
)
from fbtest.paths import AppPaths, PathOrigin, example_config_path

COMMENTED_YAML = """\
# Kopfkommentar der Datei
router:
  # Die IP-Adresse der Box
  host: 192.168.178.1
  username: ""

ping:
  # Abstand zwischen zwei Pings
  interval_s: 1.0
  targets:
    - name: fritzbox
      host: 192.168.178.1
      scope: gateway
"""


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    """Speicherorte in einem temporaeren Ordner."""
    return AppPaths(tmp_path / "config.yaml", tmp_path, PathOrigin.EXPLICIT)


@pytest.fixture
def written(paths: AppPaths) -> AppPaths:
    """Speicherorte mit einer bereits vorhandenen, kommentierten Datei."""
    paths.config_file.write_text(COMMENTED_YAML, encoding="utf-8")
    return paths


class TestFormValues:
    """Aufbereitung fuer das Formular."""

    def test_contains_all_sections(self, config: AppConfig) -> None:
        values = to_form_values(config)
        assert set(values) >= {"router", "ping", "traffic", "speedtest", "wlan", "storage"}

    def test_password_is_masked(self, config: AppConfig) -> None:
        """Das Klartextpasswort darf die Oberflaeche nie erreichen."""
        config.router.password = "geheim"
        assert to_form_values(config)["router"]["password"] == PASSWORD_MASK

    def test_empty_password_stays_empty(self, config: AppConfig) -> None:
        """Sonst saehe es im Formular so aus, als waere eines hinterlegt."""
        assert to_form_values(config)["router"]["password"] == ""

    def test_values_are_json_ready(self, config: AppConfig) -> None:
        """Pfade muessen als Text ankommen, nicht als Path-Objekt."""
        assert isinstance(to_form_values(config)["storage"]["data_dir"], str)

    def test_schema_describes_fields(self) -> None:
        schema = json_schema()
        assert schema["title"] == "AppConfig"
        assert "properties" in schema


class TestValidation:
    """Feldgenaue Fehlermeldungen."""

    def test_valid_values_pass(self, config: AppConfig) -> None:
        assert validate(to_form_values(config)).ping.targets

    def test_error_points_at_the_field(self, config: AppConfig) -> None:
        values = to_form_values(config)
        values["ping"]["interval_s"] = -1

        with pytest.raises(ConfigValidationError) as info:
            validate(values)
        assert [item.path for item in info.value.errors] == ["ping.interval_s"]
        assert info.value.errors[0].kind

    def test_error_inside_a_list_keeps_the_index(self, config: AppConfig) -> None:
        """Ohne Index waere im Formular unklar, welches Ziel gemeint ist."""
        values = to_form_values(config)
        values["ping"]["targets"][1]["scope"] = "quatsch"

        with pytest.raises(ConfigValidationError) as info:
            validate(values)
        assert info.value.errors[0].path == "ping.targets.1.scope"

    def test_collects_several_errors(self, config: AppConfig) -> None:
        values = to_form_values(config)
        values["ping"]["interval_s"] = 0
        values["router"]["timeout_s"] = -5

        with pytest.raises(ConfigValidationError) as info:
            validate(values)
        assert len(info.value.errors) == 2

    def test_unknown_key_is_rejected(self, config: AppConfig) -> None:
        """Tippfehler sollen auffallen, nicht stillschweigend wirkungslos sein."""
        values = to_form_values(config)
        values["router"]["hostname"] = "192.168.1.1"

        with pytest.raises(ConfigValidationError):
            validate(values)

    def test_raw_syntax_error(self) -> None:
        with pytest.raises(ConfigError, match="YAML-Syntaxfehler"):
            validate_raw("router: [offen\n")

    def test_raw_non_object(self) -> None:
        with pytest.raises(ConfigError, match="kein YAML-Objekt"):
            validate_raw("- eins\n- zwei\n")


class TestSaving:
    """Schreiben unter Erhalt der Datei."""

    def test_comments_survive(self, written: AppPaths, config: AppConfig) -> None:
        """Der eigentliche Grund fuer ruamel.yaml.

        Die ausgelieferte Konfiguration ist zu weiten Teilen Dokumentation.
        Wuerde das Formular sie in eine nackte Werteliste verwandeln, haette
        der Nutzer etwas verloren, ohne es zu bemerken.
        """
        values = to_form_values(config)
        values["router"]["host"] = "10.0.0.1"
        save(written, values)

        text = written.config_file.read_text(encoding="utf-8")
        assert "# Kopfkommentar der Datei" in text
        assert "# Die IP-Adresse der Box" in text
        assert "host: 10.0.0.1" in text

    def test_saved_file_is_loadable(self, written: AppPaths, config: AppConfig) -> None:
        """Was geschrieben wurde, muss die Kommandozeile wieder lesen koennen."""
        save(written, to_form_values(config))
        assert load_config(written.config_file).ping.targets

    def test_creates_file_from_template(self, paths: AppPaths, config: AppConfig) -> None:
        """Ohne Vorgaengerdatei dient die mitgelieferte Vorlage als Grundlage."""
        assert not paths.config_file.exists()
        save(paths, to_form_values(config))

        text = paths.config_file.read_text(encoding="utf-8")
        assert "#" in text, "Die neue Datei sollte die Kommentare der Vorlage enthalten"
        assert load_config(paths.config_file)

    def test_backup_is_created(self, written: AppPaths, config: AppConfig) -> None:
        result = save(written, to_form_values(config))
        assert result.backup is not None
        assert result.backup.read_text(encoding="utf-8") == COMMENTED_YAML

    def test_invalid_values_write_nothing(self, written: AppPaths, config: AppConfig) -> None:
        """Eine abgelehnte Eingabe darf die Datei nicht anfassen."""
        values = to_form_values(config)
        values["ping"]["interval_s"] = -1

        with pytest.raises(ConfigValidationError):
            save(written, values)
        assert written.config_file.read_text(encoding="utf-8") == COMMENTED_YAML

    def test_list_change_is_reported(self, written: AppPaths, config: AppConfig) -> None:
        """Bei Listen gehen Kommentare verloren - das wird gemeldet, nicht verschwiegen."""
        values = to_form_values(config)
        values["ping"]["targets"].append({"name": "neu", "host": "9.9.9.9", "scope": "internet"})
        result = save(written, values)

        assert any("ping.targets" in warning for warning in result.warnings)

    def test_removed_keys_disappear(self, written: AppPaths, config: AppConfig) -> None:
        """Sonst scheitert das naechste Laden an einem unbekannten Schluessel."""
        written.config_file.write_text(
            COMMENTED_YAML + "\nveraltet:\n  wert: 1\n", encoding="utf-8"
        )
        save(written, to_form_values(config))
        assert "veraltet" not in written.config_file.read_text(encoding="utf-8")


class TestPasswordHandling:
    """Das Passwort ist der heikelste Wert in der Datei."""

    def test_mask_does_not_erase_password(self, written: AppPaths, config: AppConfig) -> None:
        """Absenden des Formulars darf ein hinterlegtes Passwort nicht loeschen."""
        values = to_form_values(config)
        values["router"]["password"] = PASSWORD_MASK

        result = save(written, values, keep_password="geheim")
        assert result.config.router.password == "geheim"
        assert "geheim" in written.config_file.read_text(encoding="utf-8")

    def test_new_password_is_written(self, written: AppPaths, config: AppConfig) -> None:
        values = to_form_values(config)
        values["router"]["password"] = "neues-passwort"

        result = save(written, values, keep_password="altes")
        assert result.config.router.password == "neues-passwort"

    def test_password_can_be_cleared(self, written: AppPaths, config: AppConfig) -> None:
        """Ein leeres Feld loescht - nur die Maske bedeutet 'unveraendert'."""
        values = to_form_values(config)
        values["router"]["password"] = ""

        result = save(written, values, keep_password="altes")
        assert result.config.router.password == ""


class TestRawMode:
    """Rohansicht fuer Fortgeschrittene."""

    def test_read_returns_file_verbatim(self, written: AppPaths) -> None:
        assert read_raw(written) == COMMENTED_YAML

    def test_read_missing_file(self, paths: AppPaths) -> None:
        with pytest.raises(ConfigError, match="nicht lesbar"):
            read_raw(paths)

    def test_save_keeps_text_exactly(self, written: AppPaths) -> None:
        """Wer die Datei selbst formatiert, will sie so wiederfinden."""
        text = COMMENTED_YAML.replace("interval_s: 1.0", "interval_s: 2.0")
        save_raw(written, text)
        assert written.config_file.read_text(encoding="utf-8") == text

    def test_invalid_text_writes_nothing(self, written: AppPaths) -> None:
        with pytest.raises(ConfigError):
            save_raw(written, "ping:\n  targets: []\n")
        assert written.config_file.read_text(encoding="utf-8") == COMMENTED_YAML


def test_shipped_example_survives_a_round_trip(tmp_path: Path) -> None:
    """Die ausgelieferte Vorlage muss das Speichern unbeschadet ueberstehen.

    Sie ist die aufwendigste Datei des Projekts - rund 250 Zeilen, groesstenteils
    Kommentar. Wenn irgendwo Kommentare verloren gehen, dann hier.
    """
    target = tmp_path / "config.yaml"
    original = example_config_path().read_text(encoding="utf-8")
    target.write_text(original, encoding="utf-8")
    paths = AppPaths(target, tmp_path, PathOrigin.EXPLICIT)

    config = load_config(target)
    save(paths, to_form_values(config))

    text = target.read_text(encoding="utf-8")
    before = original.count("#")
    after = text.count("#")
    assert after >= before * 0.95, f"Kommentare verloren: vorher {before}, nachher {after}"
    assert load_config(target).ping.targets
