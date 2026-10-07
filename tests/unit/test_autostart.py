"""Start-with-Windows registration against a fake ``winreg`` (the real registry is never touched)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from anker_client.services.system import autostart


class _Key:
    def __init__(self, registry: FakeWinreg, path: str) -> None:
        self.registry = registry
        self.path = path

    def __enter__(self) -> _Key:
        return self

    def __exit__(self, *exc: object) -> None:
        self.registry.closed += 1


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, tuple[Any, int]]] = {}
        self.closed = 0
        self.fail_writes = False

    def OpenKey(self, hive: str, path: str, reserved: int = 0, access: int = 0) -> _Key:
        full = f"{hive}\\{path}"
        if full not in self.keys:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return _Key(self, full)

    def CreateKeyEx(self, hive: str, path: str, reserved: int = 0, access: int = 0) -> _Key:
        if self.fail_writes:
            raise PermissionError(5, "Access is denied")
        full = f"{hive}\\{path}"
        self.keys.setdefault(full, {})
        return _Key(self, full)

    def QueryValueEx(self, key: _Key, name: str) -> tuple[Any, int]:
        try:
            return self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(2, "The system cannot find the file specified") from None

    def SetValueEx(self, key: _Key, name: str, reserved: int, kind: int, value: Any) -> None:
        self.keys[key.path][name] = (value, kind)

    def DeleteValue(self, key: _Key, name: str) -> None:
        if name not in self.keys[key.path]:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.keys[key.path][name]


RUN_PATH = "HKCU\\" + autostart.RUN_KEY


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeWinreg:
    fake = FakeWinreg()
    monkeypatch.setattr(autostart, "winreg", fake)
    return fake


def test_enable_and_disable(registry: FakeWinreg) -> None:
    assert not autostart.is_enabled()
    assert autostart.set_enabled(True)
    assert autostart.is_enabled()
    value, kind = registry.keys[RUN_PATH]["AnkerClient"]
    assert value == autostart.launch_command() and kind == FakeWinreg.REG_SZ
    assert autostart.set_enabled(False)
    assert not autostart.is_enabled()
    assert "AnkerClient" not in registry.keys[RUN_PATH]
    assert autostart.set_enabled(False)  # disabling twice is fine
    assert registry.closed >= 4  # every key handle was closed


def test_disable_when_run_key_missing(registry: FakeWinreg) -> None:
    assert autostart.set_enabled(False)
    assert not autostart.is_enabled()


def test_enable_rewrites_stale_command(registry: FakeWinreg) -> None:
    registry.keys[RUN_PATH] = {"AnkerClient": ('"C:\\Old\\AnkerClient.exe" --minimized', 1)}
    assert autostart.is_enabled()
    autostart.set_enabled(True)
    assert registry.keys[RUN_PATH]["AnkerClient"][0] == autostart.launch_command()


APPROVED_PATH = "HKCU\\" + autostart.APPROVED_KEY
DISABLED_IN_TASK_MANAGER = (b"\x03\x00\x00\x00" + b"\x10\x32\x54\x76\x98\xba\xdc\x01", 3)
ENABLED_IN_TASK_MANAGER = (b"\x02" + b"\x00" * 11, 3)


def test_entry_switched_off_in_task_manager_is_reported_disabled_and_reenabled(registry: FakeWinreg) -> None:
    registry.keys[RUN_PATH] = {"AnkerClient": (autostart.launch_command(), 1)}
    registry.keys[APPROVED_PATH] = {"AnkerClient": DISABLED_IN_TASK_MANAGER, "Other": DISABLED_IN_TASK_MANAGER}
    assert not autostart.is_enabled()  # Windows will not start it, so the toggle must show "off"

    assert autostart.set_enabled(True)

    assert autostart.is_enabled()
    assert "AnkerClient" not in registry.keys[APPROVED_PATH]  # no value = enabled
    assert "Other" in registry.keys[APPROVED_PATH]  # other apps untouched


def test_entry_approved_in_task_manager_counts_as_enabled(registry: FakeWinreg) -> None:
    registry.keys[RUN_PATH] = {"AnkerClient": (autostart.launch_command(), 1)}
    registry.keys[APPROVED_PATH] = {"AnkerClient": ENABLED_IN_TASK_MANAGER}
    assert autostart.is_enabled()


def test_approval_value_without_run_value_is_not_enabled(registry: FakeWinreg) -> None:
    registry.keys[APPROVED_PATH] = {"AnkerClient": ENABLED_IN_TASK_MANAGER}
    assert not autostart.is_enabled()


def test_disable_removes_the_approval_value_too(registry: FakeWinreg) -> None:
    registry.keys[RUN_PATH] = {"AnkerClient": (autostart.launch_command(), 1)}
    registry.keys[APPROVED_PATH] = {"AnkerClient": DISABLED_IN_TASK_MANAGER}
    assert autostart.set_enabled(False)
    assert registry.keys[RUN_PATH] == {} and registry.keys[APPROVED_PATH] == {}


def test_write_failure_returns_false(registry: FakeWinreg) -> None:
    registry.fail_writes = True
    assert autostart.set_enabled(True) is False
    assert not autostart.is_enabled()


def test_no_winreg_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "winreg", None)
    assert autostart.set_enabled(True) is False
    assert autostart.is_enabled() is False


def test_launch_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\AnkerClient\AnkerClient.exe")
    assert autostart.launch_command() == r'"C:\Program Files\AnkerClient\AnkerClient.exe" --minimized'


def test_launch_command_from_source_prefers_pythonw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    expected_interpreter = tmp_path / "python.exe"
    assert autostart.launch_command() == f'"{expected_interpreter}" -m anker_client --minimized'
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert autostart.launch_command() == f'"{tmp_path / "pythonw.exe"}" -m anker_client --minimized'
