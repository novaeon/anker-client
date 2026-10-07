"""Entry-point helpers that need no Qt: argument parsing, configured log level, --version."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anker_client import __version__, app
from anker_client.core.paths import AppPaths


def test_parse_args_defaults() -> None:
    options = app.parse_args([])
    assert options == app.Options()


def test_parse_args_flags_and_qt_passthrough() -> None:
    options = app.parse_args(["--minimized", "--debug", "--reset-window", "-platform", "offscreen"])
    assert options.minimized and options.debug and options.reset_window and not options.version
    assert options.qt_args == ("-platform", "offscreen")


def test_version_flag_prints_and_exits(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "setup_logging", lambda *a, **k: pytest.fail("must not start logging"))
    assert app.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"AnkerClient {__version__}"


def test_version_without_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout", None)
    assert app.main(["--version"]) == 0


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert app.main(["--help"]) == 0
    assert "--minimized" in capsys.readouterr().out


def test_configured_log_level(tmp_path: Path) -> None:
    paths = AppPaths.under(tmp_path).ensure()
    assert app.configured_log_level(paths) == "INFO"  # no file yet
    paths.settings_file.write_text(json.dumps({"log_level": "DEBUG"}), encoding="utf-8")
    assert app.configured_log_level(paths) == "DEBUG"
    paths.settings_file.write_text(json.dumps({"log_level": "LOUD"}), encoding="utf-8")
    assert app.configured_log_level(paths) == "INFO"
    paths.settings_file.write_text("{not json", encoding="utf-8")
    assert app.configured_log_level(paths) == "INFO"
    paths.settings_file.write_text("[1, 2]", encoding="utf-8")
    assert app.configured_log_level(paths) == "INFO"


def test_app_user_model_id_is_harmless() -> None:
    assert app.set_app_user_model_id("AnkerClient.Tests") in (True, False)
