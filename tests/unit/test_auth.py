"""AuthService: keyring credentials, DPAPI-encrypted cookies, restore order, events, thread safety."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError

from anker_client.constants import KEYRING_EMAIL_KEY, KEYRING_PASSWORD_KEY, KEYRING_SERVICE
from anker_client.core import events as ev
from anker_client.core.errors import AuthError, LoginFailedError, NetworkError, OperationCancelled, SiteChangedError
from anker_client.core.models import UserInfo
from anker_client.core.paths import AppPaths
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services import _auth_dpapi
from anker_client.services.auth import SESSION_MAGIC, AuthService

try:
    import win32crypt  # type: ignore[import-not-found]  # noqa: F401

    HAVE_DPAPI = True
except Exception:
    HAVE_DPAPI = False

needs_dpapi = pytest.mark.skipif(not HAVE_DPAPI, reason="Windows DPAPI (pywin32) not available")

SESSION_VALUE = "s3cr3t-session-value"


# --- keyring backends --------------------------------------------------------------------------


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.store[(service, username)]
        except KeyError:
            raise PasswordDeleteError(username) from None


class BrokenKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringError("vault locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringError("vault locked")

    def delete_password(self, service: str, username: str) -> None:
        raise KeyringError("vault locked")


@pytest.fixture
def vault() -> Any:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def broken_vault() -> Any:
    previous = keyring.get_keyring()
    keyring.set_keyring(BrokenKeyring())
    yield
    keyring.set_keyring(previous)


# --- fake site client ----------------------------------------------------------------------------


class FakeHttp:
    def __init__(self) -> None:
        self.cookies: list[dict[str, Any]] = []
        self.imported: list[list[dict[str, Any]]] = []
        self.cleared = 0

    def export_cookies(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self.cookies]

    def import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.imported.append([dict(c) for c in cookies])
        by_name = {c["name"]: c for c in self.cookies}
        by_name.update({c["name"]: dict(c) for c in cookies})
        self.cookies = list(by_name.values())

    def clear_cookies(self) -> None:
        self.cleared += 1
        self.cookies = []


def cookie(name: str, value: str, domain: str = "ankergames.net", expires: float | None = None) -> dict[str, Any]:
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True, "expires": expires}


class FakeSite:
    """Shared server-side state: which session values are valid, the account password."""

    def __init__(self) -> None:
        self.valid_sessions: set[str] = set()
        self.password = "hunter2"
        self.display_name = "Player One"


class FakeClient:
    def __init__(self, site: FakeSite) -> None:
        self.site = site
        self.http = FakeHttp()
        self.calls: list[str] = []
        self.current_user_error: Exception | None = None
        self.login_error: Exception | None = None
        self.logout_error: Exception | None = None
        self.login_delay = 0.0

    def _session(self) -> str | None:
        return next((c["value"] for c in self.http.cookies if c["name"] == "ankergames_session"), None)

    def login(self, email: str, password: str, *, remember: bool = True, token: CancelToken | None = None) -> UserInfo:
        self.calls.append(f"login:{email}:{remember}")
        if self.login_delay:
            time.sleep(self.login_delay)
        if self.login_error is not None:
            raise self.login_error
        if password != self.site.password:
            raise LoginFailedError()
        self.site.valid_sessions.add(SESSION_VALUE)
        self.http.import_cookies([cookie("ankergames_session", SESSION_VALUE), cookie("XSRF-TOKEN", "x")])
        return UserInfo(display_name=self.site.display_name)

    def logout(self, *, token: CancelToken | None = None) -> None:
        self.calls.append("logout")
        if self.logout_error is not None:
            raise self.logout_error
        self.site.valid_sessions.discard(self._session() or "")

    def current_user(self, *, token: CancelToken | None = None) -> UserInfo | None:
        self.calls.append("current_user")
        if self.current_user_error is not None:
            raise self.current_user_error
        if self._session() in self.site.valid_sessions:
            return UserInfo(display_name=self.site.display_name)
        return None


# --- fixtures ------------------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths.under(tmp_path / "home").ensure()


@pytest.fixture
def bus() -> ev.EventBus:
    return ev.EventBus()


@pytest.fixture
def auth_events(bus: ev.EventBus) -> list[ev.AuthChanged]:
    seen: list[ev.AuthChanged] = []
    bus.subscribe(ev.AuthChanged, seen.append)
    return seen


@pytest.fixture
def settings(paths: AppPaths, bus: ev.EventBus) -> SettingsStore:
    return SettingsStore(paths.settings_file, bus)


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def client(site: FakeSite) -> FakeClient:
    return FakeClient(site)


@pytest.fixture
def auth(client: FakeClient, settings: SettingsStore, bus: ev.EventBus, paths: AppPaths, vault: MemoryKeyring
         ) -> AuthService:
    return AuthService(client, settings, bus, paths)  # type: ignore[arg-type]


@pytest.fixture
def no_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a system without pywin32: ``import win32crypt`` raises ImportError."""
    monkeypatch.setitem(sys.modules, "win32crypt", None)
    monkeypatch.setattr(_auth_dpapi, "_module", _auth_dpapi._UNSET)


@pytest.fixture(autouse=True)
def _reset_dpapi_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_auth_dpapi, "_module", _auth_dpapi._UNSET)


def restart(site: FakeSite, settings: SettingsStore, bus: ev.EventBus, paths: AppPaths) -> tuple[AuthService, FakeClient]:
    """A fresh process: new client (empty cookie jar), same files and keyring."""
    client = FakeClient(site)
    return AuthService(client, settings, bus, paths), client  # type: ignore[arg-type]


# --- DPAPI helper ---------------------------------------------------------------------------------


@needs_dpapi
def test_dpapi_round_trip() -> None:
    blob = _auth_dpapi.protect(b"hello cookies")
    assert blob is not None and b"hello cookies" not in blob
    assert _auth_dpapi.unprotect(blob) == b"hello cookies"
    assert _auth_dpapi.available()


@needs_dpapi
def test_dpapi_rejects_tampered_blob() -> None:
    blob = bytearray(_auth_dpapi.protect(b"hello") or b"")
    blob[len(blob) // 2] ^= 0xFF
    assert _auth_dpapi.unprotect(bytes(blob)) is None
    assert _auth_dpapi.unprotect(b"garbage") is None


def test_dpapi_unavailable_returns_none(no_dpapi: None) -> None:
    assert not _auth_dpapi.available()
    assert _auth_dpapi.protect(b"x") is None
    assert _auth_dpapi.unprotect(b"x") is None


# --- login -------------------------------------------------------------------------------------------


class TestLogin:
    def test_login_remembers_and_publishes(self, auth, client, vault, auth_events, paths):
        user = auth.login("  me@example.com ", "hunter2")

        assert user.display_name == "Player One"
        assert user.email == "me@example.com"
        assert auth.is_logged_in
        assert auth.user == user
        assert client.calls == ["login:me@example.com:True"]
        assert vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] == "me@example.com"
        assert vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] == "hunter2"
        assert auth.remembered_email() == "me@example.com"
        assert [e.user for e in auth_events] == [user]

    @needs_dpapi
    def test_login_persists_encrypted_cookies(self, auth, paths):
        auth.login("me@example.com", "hunter2")
        raw = paths.cookies_file.read_bytes()
        assert raw.startswith(SESSION_MAGIC)
        assert SESSION_VALUE.encode() not in raw
        assert b"ankergames_session" not in raw
        payload = json.loads(_auth_dpapi.unprotect(raw[len(SESSION_MAGIC):]))
        assert {c["name"] for c in payload["cookies"]} == {"ankergames_session", "XSRF-TOKEN"}
        assert [p for p in paths.config_dir.iterdir() if p.name.endswith(".tmp")] == []

    def test_user_is_a_copy(self, auth):
        auth.login("me@example.com", "hunter2")
        auth.user.display_name = "mutated"
        assert auth.user.display_name == "Player One"

    def test_login_without_remember_forgets(self, auth, vault, settings, paths):
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "old@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "old"
        paths.cookies_file.write_bytes(SESSION_MAGIC + b"old")

        auth.login("me@example.com", "hunter2", remember=False)

        assert vault.store == {}
        assert settings.get().remember_login is False
        assert not paths.cookies_file.exists()
        assert auth.is_logged_in

    def test_login_with_remember_turns_the_setting_back_on(self, auth, settings, vault):
        settings.update(remember_login=False)
        auth.login("me@example.com", "hunter2", remember=True)
        assert settings.get().remember_login is True
        assert vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] == "hunter2"

    def test_bad_password(self, auth, vault, auth_events):
        with pytest.raises(LoginFailedError):
            auth.login("me@example.com", "wrong")
        assert not auth.is_logged_in
        assert auth_events == []
        assert vault.store == {}

    @pytest.mark.parametrize(("email", "password"), [("", "x"), ("   ", "x"), ("me@example.com", "")])
    def test_missing_fields(self, auth, client, email, password):
        with pytest.raises(LoginFailedError):
            auth.login(email, password)
        assert client.calls == []

    def test_network_error_propagates(self, auth, client):
        client.login_error = NetworkError()
        with pytest.raises(NetworkError):
            auth.login("me@example.com", "hunter2")
        assert not auth.is_logged_in

    def test_cancelled_before_start(self, auth, client):
        token = CancelToken()
        token.cancel()
        with pytest.raises(OperationCancelled):
            auth.login("me@example.com", "hunter2", token=token)
        assert client.calls == []


# --- restore -------------------------------------------------------------------------------------------


class TestRestore:
    @needs_dpapi
    def test_restores_saved_cookies(self, auth, site, settings, bus, paths, auth_events):
        auth.login("me@example.com", "hunter2")
        auth_events.clear()

        auth2, client2 = restart(site, settings, bus, paths)
        user = auth2.restore()

        assert user is not None and user.display_name == "Player One"
        assert client2.calls == ["current_user"]  # no login needed
        assert user.email == ""  # the site does not show it and this process never knew it
        assert client2.http.imported[0][0]["value"] == SESSION_VALUE
        assert auth2.is_logged_in
        assert [e.user for e in auth_events] == [user]

    @needs_dpapi
    def test_expired_cookies_then_remembered_credentials(self, auth, site, settings, bus, paths, auth_events):
        auth.login("me@example.com", "hunter2")
        site.valid_sessions.clear()  # server forgot the session
        auth_events.clear()

        auth2, client2 = restart(site, settings, bus, paths)
        user = auth2.restore()

        assert user is not None
        assert client2.calls == ["current_user", "login:me@example.com:True"]
        assert len(auth_events) == 1 and auth_events[0].user == user

    @needs_dpapi
    def test_expired_cookies_without_credentials(self, auth, site, settings, bus, paths, vault, auth_events):
        auth.login("me@example.com", "hunter2")
        vault.store.clear()
        site.valid_sessions.clear()
        auth_events.clear()

        auth2, client2 = restart(site, settings, bus, paths)

        assert auth2.restore() is None
        assert client2.calls == ["current_user"]
        assert not paths.cookies_file.exists()  # an expired session is not kept
        assert auth_events == []

    def test_no_saved_session_uses_credentials(self, auth, client, vault):
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "me@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "hunter2"
        user = auth.restore()
        assert user is not None and user.email == "me@example.com"
        assert client.calls == ["login:me@example.com:True"]  # no point asking current_user without cookies

    def test_nothing_remembered(self, auth, client, auth_events):
        assert auth.restore() is None
        assert client.calls == []
        assert auth_events == []

    def test_rejected_password_is_forgotten(self, auth, client, vault):
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "me@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "changed-on-the-website"
        assert auth.restore() is None
        assert (KEYRING_SERVICE, KEYRING_PASSWORD_KEY) not in vault.store
        assert auth.remembered_email() == "me@example.com"

    @needs_dpapi
    @pytest.mark.parametrize("error", [NetworkError(), SiteChangedError()])
    def test_errors_on_current_user_are_not_raised(self, auth, site, settings, bus, paths, error):
        auth.login("me@example.com", "hunter2")
        auth2, client2 = restart(site, settings, bus, paths)
        client2.current_user_error = error
        assert auth2.restore() is None
        assert client2.calls == ["current_user"]
        assert paths.cookies_file.exists()  # still valid for the next attempt

    def test_network_error_on_login_is_not_raised(self, auth, client, vault):
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "me@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "hunter2"
        client.login_error = NetworkError()
        assert auth.restore() is None
        assert vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] == "hunter2"  # kept

    @needs_dpapi
    def test_remember_off_skips_everything(self, auth, site, settings, bus, paths, vault):
        auth.login("me@example.com", "hunter2")
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "me@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "hunter2"
        settings._settings.remember_login = False  # as if edited while the app was closed

        auth2, client2 = restart(site, settings, bus, paths)

        assert auth2.restore() is None
        assert client2.calls == []

    def test_cancellation_propagates(self, auth, vault):
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "me@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "hunter2"
        token = CancelToken()
        token.cancel()
        with pytest.raises(OperationCancelled):
            auth.restore(token=token)

    def test_corrupt_session_file_is_ignored(self, auth, client, paths, vault):
        paths.cookies_file.write_bytes(SESSION_MAGIC + b"not a dpapi blob")
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "me@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "hunter2"
        assert auth.restore() is not None
        assert client.calls == ["login:me@example.com:True"]

    def test_foreign_file_is_ignored(self, auth, client, paths):
        paths.cookies_file.write_bytes(b'{"cookies": []}')
        assert auth.restore() is None
        assert client.calls == []

    @needs_dpapi
    def test_restore_while_signed_in_reverifies(self, auth, client, site, auth_events):
        auth.login("me@example.com", "hunter2", remember=False)
        auth_events.clear()
        client.calls.clear()
        restored = auth.restore()
        assert restored is not None and restored.email == "me@example.com"
        assert client.calls == ["current_user"]
        assert auth_events == []  # nothing changed
        client.current_user_error = NetworkError()
        assert auth.restore() == restored  # offline: keep the live session
        assert auth_events == []
        client.current_user_error = None
        site.valid_sessions.clear()
        assert auth.restore() is None
        assert not auth.is_logged_in
        assert [e.user for e in auth_events] == [None]

    @needs_dpapi
    def test_expired_cookies_are_not_restored(self, auth, site, settings, bus, paths, client):
        client.http.cookies.append(cookie("old", "x", expires=time.time() - 60))
        client.http.cookies.append(cookie("fresh", "y", expires=time.time() + 3600))
        auth.login("me@example.com", "hunter2")
        auth2, client2 = restart(site, settings, bus, paths)
        auth2.restore()
        names = {c["name"] for c in client2.http.imported[0]}
        assert names == {"ankergames_session", "XSRF-TOKEN", "fresh"}


# --- cookies sign-in / logout / save ------------------------------------------------------------------


class TestCookiesLogin:
    @needs_dpapi
    def test_login_with_browser_cookies(self, auth, client, site, paths, auth_events):
        site.valid_sessions.add("discord-session")
        captured = [
            cookie("ankergames_session", "discord-session", domain=".ankergames.net"),
            cookie("discord_token", "do-not-keep", domain="discord.com"),
            cookie("cf_clearance", "cf", domain="www.ankergames.net"),
        ]
        user = auth.login_with_cookies(captured)

        assert user.display_name == "Player One"
        assert {c["name"] for c in client.http.imported[0]} == {"ankergames_session", "cf_clearance"}
        assert [e.user for e in auth_events] == [user]
        payload = json.loads(_auth_dpapi.unprotect(paths.cookies_file.read_bytes()[len(SESSION_MAGIC):]))
        assert "discord_token" not in {c["name"] for c in payload["cookies"]}

    def test_rejected_cookies(self, auth, auth_events):
        with pytest.raises(AuthError):
            auth.login_with_cookies([cookie("ankergames_session", "unknown")])
        with pytest.raises(AuthError):
            auth.login_with_cookies([cookie("other", "x", domain="discord.com")])
        with pytest.raises(AuthError):
            auth.login_with_cookies([])
        assert not auth.is_logged_in
        assert auth_events == []


class TestLogout:
    def test_logout(self, auth, client, vault, paths, auth_events):
        auth.login("me@example.com", "hunter2")
        auth_events.clear()

        auth.logout()

        assert "logout" in client.calls
        assert client.http.cleared == 1
        assert not paths.cookies_file.exists()
        assert (KEYRING_SERVICE, KEYRING_PASSWORD_KEY) not in vault.store
        assert auth.remembered_email() == "me@example.com"  # kept for pre-filling the form
        assert not auth.is_logged_in and auth.user is None
        assert [e.user for e in auth_events] == [None]

    @pytest.mark.parametrize("error", [NetworkError(), OperationCancelled()])
    def test_logout_always_works_locally(self, auth, client, auth_events, error):
        auth.login("me@example.com", "hunter2")
        client.logout_error = error
        auth.logout()
        assert not auth.is_logged_in
        assert auth_events[-1].user is None

    def test_restore_after_logout_does_not_sign_in(self, auth, site, settings, bus, paths):
        auth.login("me@example.com", "hunter2")
        auth.logout()
        auth2, client2 = restart(site, settings, bus, paths)
        assert auth2.restore() is None
        assert client2.calls == []


class TestSaveSession:
    def test_not_signed_in_keeps_existing_file(self, auth, paths):
        paths.cookies_file.write_bytes(SESSION_MAGIC + b"previous")
        auth.save_session()
        assert paths.cookies_file.read_bytes() == SESSION_MAGIC + b"previous"

    def test_remember_off_deletes_file(self, auth, settings, paths):
        paths.cookies_file.write_bytes(SESSION_MAGIC + b"previous")
        settings._settings.remember_login = False
        auth.save_session()
        assert not paths.cookies_file.exists()

    @needs_dpapi
    def test_turning_remember_off_forgets_everything(self, auth, settings, vault, paths):
        auth.login("me@example.com", "hunter2")
        assert paths.cookies_file.exists()
        settings.update(remember_login=False)
        assert vault.store == {}
        assert not paths.cookies_file.exists()
        assert auth.is_logged_in  # still signed in for this run

    def test_without_dpapi_nothing_is_persisted(self, auth, paths, no_dpapi, caplog):
        caplog.set_level(logging.WARNING, logger="anker_client.services.auth")
        auth.login("me@example.com", "hunter2")
        auth.save_session()
        auth.save_session()
        assert auth.is_logged_in
        assert not paths.cookies_file.exists()
        warnings = [r for r in caplog.records if "cannot be encrypted" in r.getMessage()]
        assert len(warnings) == 1

    def test_without_dpapi_restore_falls_back_to_credentials(self, auth, client, paths, vault, no_dpapi):
        paths.cookies_file.write_bytes(SESSION_MAGIC + b"blob from a dpapi system")
        vault.store[(KEYRING_SERVICE, KEYRING_EMAIL_KEY)] = "me@example.com"
        vault.store[(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)] = "hunter2"
        assert auth.restore() is not None
        assert client.calls == ["login:me@example.com:True"]


class TestKeyringFailures:
    def test_broken_keyring_is_never_fatal(self, client, settings, bus, paths, broken_vault):
        auth = AuthService(client, settings, bus, paths)  # type: ignore[arg-type]
        assert auth.remembered_email() == ""
        assert auth.login("me@example.com", "hunter2").display_name == "Player One"
        auth.restore()
        auth.logout()
        assert not auth.is_logged_in


class RealHttpClient(FakeClient):
    """FakeClient whose cookie jar is the production ``HttpClient`` (export/import shape, domains, expiry)."""

    def __init__(self, site: FakeSite) -> None:
        from anker_client.site.http import HttpClient

        super().__init__(site)
        self.http = HttpClient()  # type: ignore[assignment]

    def _session(self) -> str | None:
        return self.http.cookie("ankergames_session")  # type: ignore[attr-defined]


@needs_dpapi
def test_session_round_trip_through_the_real_http_client(site, settings, bus, paths, vault):
    client = RealHttpClient(site)
    auth = AuthService(client, settings, bus, paths)  # type: ignore[arg-type]
    auth.login("me@example.com", "hunter2")
    client.http.import_cookies([cookie("tracker", "t", domain="ads.example.com")])  # never persisted
    auth.save_session()

    client2 = RealHttpClient(site)
    auth2 = AuthService(client2, settings, bus, paths)  # type: ignore[arg-type]
    user = auth2.restore()

    assert user is not None and user.display_name == "Player One"
    assert client2.calls == ["current_user"]
    assert client2.http.cookie("ankergames_session") == SESSION_VALUE  # type: ignore[attr-defined]
    assert client2.http.cookie("tracker") is None  # type: ignore[attr-defined]
    client.http.close()  # type: ignore[attr-defined]
    client2.http.close()  # type: ignore[attr-defined]


class TestConcurrentSignIn:
    """A background restore() must yield to sign-ins/sign-outs the user makes while it runs."""

    @staticmethod
    def _block(target: Any, name: str, result: Any = None) -> tuple[threading.Event, threading.Event]:
        """Make ``target.name`` wait for a gate on its first call, then answer ``result`` (or the original)."""
        entered, gate = threading.Event(), threading.Event()
        original = getattr(target, name)
        calls = {"n": 0}

        def blocked(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                gate.wait(5)
                return result(*args, **kwargs) if callable(result) else original(*args, **kwargs)
            return original(*args, **kwargs)

        setattr(target, name, blocked)
        return entered, gate

    @needs_dpapi
    def test_restore_does_not_sign_out_a_user_who_signed_in_meanwhile(self, auth, site, settings, bus, paths,
                                                                       vault, auth_events):
        auth.login("me@example.com", "hunter2")  # a previous run saved a session…
        site.valid_sessions.clear()  # …that has expired on the server since
        vault.store.clear()
        auth2, client2 = restart(site, settings, bus, paths)
        entered, gate = self._block(client2, "current_user", result=lambda **kw: None)
        auth_events.clear()

        thread = threading.Thread(target=auth2.restore)
        thread.start()
        assert entered.wait(5)
        user = auth2.login("me@example.com", "hunter2", remember=False)  # the user signs in meanwhile
        gate.set()
        thread.join(5)

        assert auth2.is_logged_in and auth2.user == user
        assert [e.user for e in auth_events] == [user]
        assert client2.calls.count("login:me@example.com:False") == 1

    @needs_dpapi
    def test_restore_keeps_the_session_file_saved_meanwhile(self, auth, site, settings, bus, paths, vault):
        auth.login("me@example.com", "hunter2")
        site.valid_sessions.clear()
        auth2, client2 = restart(site, settings, bus, paths)
        entered, gate = self._block(client2, "current_user", result=lambda **kw: None)

        thread = threading.Thread(target=auth2.restore)
        thread.start()
        assert entered.wait(5)
        auth2.login("me@example.com", "hunter2")  # saves a fresh session
        fresh = paths.cookies_file.read_bytes()
        gate.set()
        thread.join(5)

        assert paths.cookies_file.read_bytes() == fresh
        assert [c for c in client2.calls if c.startswith("login")] == ["login:me@example.com:True"]

    def test_restore_does_not_revive_a_user_who_signed_out_meanwhile(self, auth, client, auth_events):
        auth.login("me@example.com", "hunter2", remember=False)
        entered, gate = self._block(client, "current_user")  # answers with the (still valid) user

        thread = threading.Thread(target=auth.restore)
        thread.start()
        assert entered.wait(5)
        auth.logout()
        gate.set()
        thread.join(5)

        assert not auth.is_logged_in
        assert auth_events[-1].user is None

    def test_background_sign_in_with_credentials_yields_to_a_sign_out(self, auth, client, site, vault, auth_events):
        auth.login("me@example.com", "hunter2")
        site.valid_sessions.clear()  # the live session expired → restore falls back to the password
        entered, gate = self._block(client, "login")

        thread = threading.Thread(target=auth.restore)
        thread.start()
        assert entered.wait(5)
        auth.logout()
        gate.set()
        thread.join(5)

        assert not auth.is_logged_in
        assert auth_events[-1].user is None
        assert (KEYRING_SERVICE, KEYRING_PASSWORD_KEY) not in vault.store

    @needs_dpapi
    def test_save_session_does_not_write_cookies_captured_before_a_logout(self, auth, paths, monkeypatch):
        auth.login("me@example.com", "hunter2")
        entered, gate = threading.Event(), threading.Event()
        real_protect = _auth_dpapi.protect

        def slow_protect(data: bytes) -> bytes | None:
            entered.set()
            gate.wait(5)
            return real_protect(data)

        monkeypatch.setattr(_auth_dpapi, "protect", slow_protect)
        thread = threading.Thread(target=auth.save_session)  # e.g. at shutdown
        thread.start()
        assert entered.wait(5)
        auth.logout()
        gate.set()
        thread.join(5)

        assert not paths.cookies_file.exists()

    def test_logout_signs_out_locally_before_the_network_call(self, auth, client):
        auth.login("me@example.com", "hunter2")
        seen: list[bool] = []
        original = client.logout

        def logout(**kwargs: Any) -> None:
            seen.append(auth.is_logged_in)
            original(**kwargs)

        client.logout = logout  # type: ignore[method-assign]
        auth.logout()
        assert seen == [False]

    def test_logout_survives_unexpected_client_errors(self, auth, client, paths, auth_events):
        auth.login("me@example.com", "hunter2")
        client.logout_error = RuntimeError("parser bug")
        auth.logout()
        assert not auth.is_logged_in
        assert client.http.cleared == 1
        assert not paths.cookies_file.exists()
        assert auth_events[-1].user is None


def test_login_succeeds_when_the_settings_file_cannot_be_written(auth, settings, monkeypatch, vault):
    def fail(**changes: Any) -> Any:
        raise PermissionError("config.json is read-only")

    monkeypatch.setattr(settings, "update", fail)
    user = auth.login("me@example.com", "hunter2", remember=False)
    assert user.display_name == "Player One"
    assert auth.is_logged_in
    assert vault.store == {}  # remember=False still forgets the credentials


def test_user_property_is_thread_safe(auth, client):
    stop = threading.Event()
    errors: list[BaseException] = []
    reads = 0

    def reader() -> None:
        nonlocal reads
        while not stop.is_set():
            try:
                user = auth.user
                assert user is None or user.display_name == "Player One"
                _ = auth.is_logged_in
                reads += 1
            except BaseException as exc:
                errors.append(exc)
                return
            time.sleep(0)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    for _ in range(10):
        auth.login("me@example.com", "hunter2", remember=False)  # no session file I/O
        auth.logout()
    stop.set()
    for t in readers:
        t.join(5)
    assert errors == []
    assert reads > 0
