"""Tests der Pfadauflösung.

Die Regeln entscheiden darueber, wo die Messdatenbank liegt. Ein Fehler hier
faellt nicht sofort auf, sondern erst, wenn ein Nutzer seine Testlaeufe nicht
mehr findet - deshalb ist jede Regel einzeln geprueft, samt ihrer Rangfolge.

Alle Tests speisen Arbeitsverzeichnis, Umgebung und Bundle-Zustand ein und
kommen ohne echte Benutzerverzeichnisse aus.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fbtest.paths import (
    APP_DIR_NAME,
    CONFIG_NAME,
    HOME_ENV,
    AppPaths,
    PathOrigin,
    bundle_dir,
    example_config_path,
    looks_like_project,
    resolve_paths,
    resource_path,
    user_config_dir,
    user_data_dir,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Ein Ordner, der wie der Quellcode-Projektordner aussieht."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fbtest'\n", encoding="utf-8")
    (tmp_path / "src" / APP_DIR_NAME).mkdir(parents=True)
    return tmp_path


class TestResourcePaths:
    """Mitgelieferte Dateien muessen im Quellbaum auffindbar sein."""

    def test_bundle_dir_is_package_dir(self) -> None:
        assert (bundle_dir() / "paths.py").is_file()

    @pytest.mark.parametrize(
        "parts",
        [
            ("dashboard", "static", "index.html"),
            ("report", "templates"),
            ("resources", "config.example.yaml"),
        ],
    )
    def test_shipped_files_exist(self, parts: tuple[str, ...]) -> None:
        """Genau diese Pfade muss auch das Bundle mitbringen."""
        assert resource_path(*parts).exists(), f"Fehlt: {'/'.join(parts)}"

    def test_example_config_is_inside_package(self) -> None:
        """Die Vorlage muss im Paket liegen, nicht im Projektordner.

        Im gebuendelten Programm gibt es keinen Projektordner - laege die Datei
        dort, koennte die Anwendung sich nicht selbst einrichten.
        """
        assert example_config_path().is_relative_to(bundle_dir())


class TestProjectDetection:
    """Erkennung des Quellcode-Projektordners."""

    def test_detects_project(self, project: Path) -> None:
        assert looks_like_project(project)

    def test_ignores_foreign_python_project(self, tmp_path: Path) -> None:
        """Ein fremdes Python-Projekt darf nicht als fbtest-Projekt gelten.

        Sonst legte 'fbtest init' im erstbesten Projektordner eine config.yaml an.
        """
        (tmp_path / "pyproject.toml").write_text("[project]\nname='andere'\n", encoding="utf-8")
        assert not looks_like_project(tmp_path)

    def test_ignores_empty_directory(self, tmp_path: Path) -> None:
        assert not looks_like_project(tmp_path)


class TestResolveOrder:
    """Rangfolge der fuenf Regeln."""

    def test_explicit_option_wins(self, project: Path, tmp_path: Path) -> None:
        """--config schlaegt alles andere, auch eine vorhandene config.yaml."""
        (project / CONFIG_NAME).write_text("ping: {}\n", encoding="utf-8")
        wanted = tmp_path / "woanders" / "eigene.yaml"
        paths = resolve_paths(wanted, cwd=project, environ={HOME_ENV: str(tmp_path)})
        assert paths.config_file == wanted.resolve()
        assert paths.base_dir == wanted.resolve().parent
        assert paths.origin is PathOrigin.EXPLICIT

    def test_relative_option_is_resolved_against_cwd(self, project: Path) -> None:
        paths = resolve_paths(Path("unter/eigene.yaml"), cwd=project, environ={})
        assert paths.config_file == (project / "unter" / "eigene.yaml").resolve()

    def test_environment_beats_working_dir(self, project: Path, tmp_path: Path) -> None:
        (project / CONFIG_NAME).write_text("ping: {}\n", encoding="utf-8")
        home = tmp_path / "vorgabe"
        paths = resolve_paths(cwd=project, environ={HOME_ENV: str(home)})
        assert paths.base_dir == home.resolve()
        assert paths.origin is PathOrigin.ENVIRONMENT

    def test_blank_environment_is_ignored(self, project: Path) -> None:
        """Eine leer gesetzte Variable darf nicht in den Wurzelordner zeigen."""
        paths = resolve_paths(cwd=project, environ={HOME_ENV: "   "})
        assert paths.origin is PathOrigin.PROJECT_DIR

    def test_existing_config_in_working_dir(self, tmp_path: Path) -> None:
        """Die wichtigste Regel: bestehende Installationen behalten ihre Daten."""
        (tmp_path / CONFIG_NAME).write_text("ping: {}\n", encoding="utf-8")
        paths = resolve_paths(cwd=tmp_path, environ={})
        assert paths.base_dir == tmp_path.resolve()
        assert paths.origin is PathOrigin.WORKING_DIR
        assert paths.exists

    def test_working_dir_wins_even_when_frozen(self, tmp_path: Path) -> None:
        """Auch die gebuendelte App nimmt eine danebenliegende config.yaml."""
        (tmp_path / CONFIG_NAME).write_text("ping: {}\n", encoding="utf-8")
        paths = resolve_paths(cwd=tmp_path, environ={}, frozen=True)
        assert paths.origin is PathOrigin.WORKING_DIR

    def test_project_dir_without_config(self, project: Path) -> None:
        """'fbtest init' im frisch geklonten Projekt bleibt im Projektordner."""
        paths = resolve_paths(cwd=project, environ={})
        assert paths.config_file == project.resolve() / CONFIG_NAME
        assert paths.origin is PathOrigin.PROJECT_DIR
        assert not paths.exists

    def test_frozen_app_ignores_project_dir(self, project: Path) -> None:
        """Im Bundle ist das Programmverzeichnis schreibgeschuetzt.

        Ein Projektordner im Arbeitsverzeichnis waere dort ein Zufallstreffer
        und darf die Daten nicht an sich ziehen.
        """
        paths = resolve_paths(cwd=project, environ={"LOCALAPPDATA": str(project / "appdata")},
                              frozen=True)
        assert paths.origin is PathOrigin.USER_DIR

    def test_falls_back_to_user_dir(self, tmp_path: Path) -> None:
        env = {"LOCALAPPDATA": str(tmp_path / "lokal"),
               "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
               "XDG_DATA_HOME": str(tmp_path / "dat")}
        paths = resolve_paths(cwd=tmp_path / "leer", environ=env)
        assert paths.origin is PathOrigin.USER_DIR
        assert paths.config_file.name == CONFIG_NAME
        assert APP_DIR_NAME in paths.config_file.parts


class TestUserDirectories:
    """Plattformuebliche Ablageorte."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-spezifisch")
    def test_windows_prefers_local_appdata(self, tmp_path: Path) -> None:
        """Roaming-Profile duerfen keine Messdatenbank synchronisieren."""
        env = {"LOCALAPPDATA": str(tmp_path / "lokal"), "APPDATA": str(tmp_path / "roaming")}
        assert user_config_dir(env) == tmp_path / "lokal" / APP_DIR_NAME
        assert user_data_dir(env) == user_config_dir(env)

    @pytest.mark.skipif(sys.platform == "win32", reason="XDG-spezifisch")
    def test_xdg_separates_config_and_data(self, tmp_path: Path) -> None:
        env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "dat")}
        assert user_config_dir(env) == tmp_path / "cfg" / APP_DIR_NAME
        assert user_data_dir(env) == tmp_path / "dat" / APP_DIR_NAME


class TestDescription:
    """Die Begruendung wird in der Diagnose angezeigt."""

    @pytest.mark.parametrize("origin", list(PathOrigin))
    def test_every_origin_has_a_description(self, origin: PathOrigin, tmp_path: Path) -> None:
        paths = AppPaths(tmp_path / CONFIG_NAME, tmp_path, origin)
        assert paths.describe()
